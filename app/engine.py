"""seedmo – torrent-motoren (libtorrent).

Holder én libtorrent-session, gemmer resume-data så torrents overlever en
genstart, og stopper seeding når ratio- eller tidsgrænsen er nået.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path

import libtorrent as lt

log = logging.getLogger("seedmo")

DEFAULT_SETTINGS = {
    "download_limit_kb": 0,      # 0 = ubegrænset
    "upload_limit_kb": 0,        # 0 = ubegrænset
    "seed_ratio_limit": 1.0,     # 0 = stop straks når hentet, -1 = ingen grænse
    "seed_time_limit_min": 0,    # 0 = ingen tidsgrænse
    "max_active_downloads": 5,
    "start_paused": False,       # vælg filer før der hentes
    "limit_lan": False,          # gælder grænserne også peers på det lokale netværk?
}


def _secs(v) -> float:
    if isinstance(v, timedelta):
        return v.total_seconds()
    try:
        return float(v)
    except Exception:
        return 0.0


def _tid(obj) -> str:
    ih = obj.info_hashes
    if callable(ih):  # torrent_handle har en metode, torrent_status et felt
        ih = ih()
    return str(ih.get_best())


def _safe_rel(path: str) -> str:
    """Relativ undermappe under downloads – ingen '..' eller absolutte stier."""
    parts = [p for p in Path(path.strip().replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
    return str(Path(*parts)) if parts else ""


class Engine:
    def __init__(self, config_dir: Path, download_dir: Path, peer_port: int = 6881):
        self.config_dir = Path(config_dir)
        self.download_dir = Path(download_dir)
        self.resume_dir = self.config_dir / "resume"
        self.resume_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.config_dir / "settings.json"
        self.meta_file = self.config_dir / "meta.json"
        self.lock = threading.RLock()
        self.settings = dict(DEFAULT_SETTINGS)
        if self.settings_file.exists():
            try:
                self.settings.update(json.loads(self.settings_file.read_text()))
            except Exception:
                log.exception("Kunne ikke læse settings.json – bruger standard")
        self.meta: dict[str, dict] = {}
        self.events: list[str] = []  # seneste advarsler/fejl, vises i /api/diag
        if self.meta_file.exists():
            try:
                self.meta = json.loads(self.meta_file.read_text())
            except Exception:
                self.meta = {}

        pack = {
            "listen_interfaces": f"0.0.0.0:{peer_port},[::]:{peer_port}",
            "alert_mask": lt.alert_category.error | lt.alert_category.status | lt.alert_category.storage,
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "user_agent": "seedmo/1.0 libtorrent/" + lt.__version__,
            # Behold forbindelser mens en torrent venter på filvalg (alle filer fravalgt
            # tæller som "færdig", og så ville libtorrent ellers lukke til seeds)
            "close_redundant_connections": False,
            # Skriv med almindelig pwrite i stedet for mmap. mmap-skrivning kan gå i stå
            # på NAS-diske og i Docker (download stopper efter få KB).
            "disk_write_mode": 0,
        }
        self.ses = lt.session(pack)
        self._apply_settings()
        self._stop = threading.Event()
        self._load_resume()
        self._threads = [
            threading.Thread(target=self._alert_loop, daemon=True),
            threading.Thread(target=self._housekeeping_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()

    # ---------- indstillinger ----------
    def _apply_peer_classes(self, limit_lan: bool):
        """libtorrent undtager som standard lokale peers fra hastighedsgrænserne."""
        f = lt.ip_filter()
        g = 1 << lt.session.global_peer_class_id
        loc = 1 << lt.session.local_peer_class_id
        f.add_rule("0.0.0.0", "255.255.255.255", g)
        f.add_rule("::", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", g)
        if not limit_lan:
            for a, b in [("10.0.0.0", "10.255.255.255"), ("172.16.0.0", "172.31.255.255"),
                         ("192.168.0.0", "192.168.255.255"), ("169.254.0.0", "169.254.255.255"),
                         ("127.0.0.0", "127.255.255.255"), ("fe80::", "febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff"),
                         ("fc00::", "fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"), ("::1", "::1")]:
                f.add_rule(a, b, loc)
        self.ses.set_peer_class_filter(f)

    def _apply_settings(self):
        s = self.settings
        self._apply_peer_classes(bool(s.get("limit_lan")))
        self.ses.apply_settings({
            "download_rate_limit": max(0, int(s["download_limit_kb"])) * 1024,
            "upload_rate_limit": max(0, int(s["upload_limit_kb"])) * 1024,
            "active_downloads": max(1, int(s["max_active_downloads"])),
            "active_seeds": 50,
            "active_limit": 200,
        })

    def get_settings(self) -> dict:
        return dict(self.settings)

    def update_settings(self, data: dict) -> dict:
        with self.lock:
            for k, default in DEFAULT_SETTINGS.items():
                if k in data and data[k] is not None:
                    self.settings[k] = type(default)(data[k])
            self.settings_file.write_text(json.dumps(self.settings, indent=2))
            self._apply_settings()
        return self.get_settings()

    def _save_meta(self):
        tmp = self.meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.meta, indent=2))
        tmp.replace(self.meta_file)

    # ---------- tilføj ----------
    def _add(self, params: lt.add_torrent_params, folder: str = "") -> str:
        rel = _safe_rel(folder)
        params.save_path = str(self.download_dir / rel) if rel else str(self.download_dir)
        choose = bool(self.settings.get("start_paused"))
        params.flags |= lt.torrent_flags.auto_managed
        params.flags &= ~lt.torrent_flags.paused
        if choose:
            # Ingen filer hentes, før brugeren har valgt dem. Metadata hentes stadig,
            # så fillisten kan vises – også for magnet-links.
            params.flags |= lt.torrent_flags.default_dont_download
        params.flags &= ~lt.torrent_flags.duplicate_is_error
        with self.lock:
            h = self.ses.add_torrent(params)
            tid = _tid(h)
            m = self.meta.setdefault(tid, {"added": time.time(), "folder": rel})
            if choose and not m.get("choose_done"):
                m["awaiting_selection"] = True
            self._save_meta()
            h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
        return tid

    def add_magnet(self, uri: str, folder: str = "") -> str:
        uri = uri.strip()
        if not uri.startswith("magnet:?"):
            raise ValueError("Det ligner ikke et magnet-link")
        return self._add(lt.parse_magnet_uri(uri), folder)

    def add_torrent_file(self, data: bytes, folder: str = "") -> str:
        try:
            ti = lt.torrent_info(lt.bdecode(data))
        except Exception as e:
            raise ValueError("Filen er ikke en gyldig .torrent-fil") from e
        p = lt.add_torrent_params()
        p.ti = ti
        return self._add(p, folder)

    # ---------- status ----------
    def _status_dict(self, s) -> dict:
        tid = _tid(s)
        m = self.meta.get(tid, {})
        paused = bool(s.flags & lt.torrent_flags.paused)
        finished = s.is_finished or s.is_seeding
        if s.errc.value() != 0:
            stage, word, kind = 0, "Fejl: " + s.errc.message(), "problem"
        elif not s.has_metadata:
            stage, word, kind = 1, "Henter metadata", "pending"
        elif m.get("awaiting_selection"):
            stage, word, kind = 1, "Vælg filer", "attention"
        elif s.state == lt.torrent_status.checking_files or s.state == lt.torrent_status.checking_resume_data:
            stage, word, kind = 2, "Kontrollerer", "transit"
        elif not finished:
            stage, word, kind = 2, "Henter", "transit"
        elif m.get("seed_done") or (paused and finished):
            stage, word, kind = 4, "Færdig", "done"
        else:
            stage, word, kind = 3, "Seeder", "ready"
        if paused and stage in (1, 2):
            queued = bool(s.flags & lt.torrent_flags.auto_managed)
            word, kind = ("I kø", "pending") if queued else ("Sat på pause", "attention")
        if s.flags & lt.torrent_flags.upload_mode and stage in (1, 2):
            word, kind = "Diskfejl – venter", "problem"
        wanted = s.total_wanted or 0
        done = s.total_wanted_done or 0
        # libtorrent viser et glidende gennemsnit – vis 0 når der ikke hentes/deles
        rate = 0 if (paused or finished) else s.download_payload_rate
        up_rate = 0 if paused else s.upload_payload_rate
        eta = int((wanted - done) / rate) if rate > 0 and wanted > done else None
        ratio = (s.all_time_upload / wanted) if wanted else 0.0
        return {
            "id": tid,
            "name": s.name or (m.get("name") or tid[:12]),
            "stage": stage,
            "status": word,
            "kind": kind,
            "paused": paused,
            "progress": round(s.progress, 4),
            "size": wanted,
            "total_size": self._total_size(s),
            "done": done,
            "down_rate": rate,
            "up_rate": up_rate,
            "uploaded": s.all_time_upload,
            "ratio": round(ratio, 2),
            "peers": s.num_peers,
            "seeds": s.num_seeds,
            "eta": eta,
            "has_metadata": s.has_metadata,
            "added": m.get("added"),
            "folder": m.get("folder", ""),
            "seeding_seconds": int(_secs(s.seeding_duration)),
        }

    @staticmethod
    def _total_size(s) -> int:
        try:
            ti = s.torrent_file
            return ti.total_size() if ti is not None else 0
        except Exception:
            return 0

    def list(self) -> list[dict]:
        with self.lock:
            out = [self._status_dict(h.status()) for h in self.ses.get_torrents()]
        out.sort(key=lambda t: t.get("added") or 0, reverse=True)
        return out

    def _handle(self, tid: str):
        for h in self.ses.get_torrents():
            if _tid(h) == tid:
                return h
        raise KeyError(tid)

    def detail(self, tid: str) -> dict:
        with self.lock:
            h = self._handle(tid)
            s = h.status()
            d = self._status_dict(s)
            files = []
            if s.has_metadata:
                fs = h.torrent_file().files()
                prios = h.get_file_priorities()
                prog = h.file_progress(lt.torrent_handle.piece_granularity)
                for i in range(fs.num_files()):
                    if fs.file_flags(i) & lt.file_storage.flag_pad_file:
                        continue
                    size = fs.file_size(i)
                    files.append({
                        "index": i,
                        "path": fs.file_path(i),
                        "size": size,
                        "done": prog[i] if i < len(prog) else 0,
                        "wanted": (prios[i] if i < len(prios) else 4) > 0,
                    })
            d["files"] = files
            d["save_path"] = s.save_path
            d["magnet"] = lt.make_magnet_uri(h) if s.has_metadata else None
        return d

    # ---------- handlinger ----------
    def set_files(self, tid: str, wanted: dict[int, bool]):
        with self.lock:
            h = self._handle(tid)
            prios = list(h.get_file_priorities())
            for i, w in wanted.items():
                i = int(i)
                if 0 <= i < len(prios):
                    prios[i] = 4 if w else 0
            h.prioritize_files(prios)
            # Flere filer valgt efter færdig → hent videre
            m = self.meta.setdefault(tid, {})
            m["seed_done"] = False
            if m.pop("awaiting_selection", None):
                m["choose_done"] = True
                # Find peers igen med det samme i stedet for at vente på næste announce
                h.force_reannounce()
                h.force_dht_announce()
            self._save_meta()
            h.save_resume_data()

    def pause(self, tid: str):
        with self.lock:
            h = self._handle(tid)
            h.unset_flags(lt.torrent_flags.auto_managed)
            h.pause()
            h.save_resume_data()

    def resume(self, tid: str):
        with self.lock:
            h = self._handle(tid)
            m = self.meta.setdefault(tid, {})
            # Starter man en færdig torrent igen, nulstilles seed-stop, så den seeder
            if m.get("seed_done"):
                m["seed_done"] = False
                m["seed_override"] = True
                self._save_meta()
            h.set_flags(lt.torrent_flags.auto_managed)
            h.unset_flags(lt.torrent_flags.upload_mode)  # fx efter fuld disk
            h.resume()
            h.save_resume_data()

    def remove(self, tid: str, delete_files: bool = False):
        with self.lock:
            h = self._handle(tid)
            flags = lt.session.delete_files if delete_files else 0
            self.ses.remove_torrent(h, flags)
            (self.resume_dir / f"{tid}.fastresume").unlink(missing_ok=True)
            self.meta.pop(tid, None)
            self._save_meta()

    def diag(self) -> dict:
        """Fejlsøgning: status pr. torrent og peers' tilstand."""
        out = []
        with self.lock:
            for h in self.ses.get_torrents():
                s = h.status()
                peers = h.get_peer_info()
                out.append({
                    "name": s.name, "state": str(s.state), "error": s.errc.message() if s.errc.value() else "",
                    "progress": round(s.progress, 4), "peers": s.num_peers, "seeds": s.num_seeds,
                    "connect_candidates": s.connect_candidates,
                    "interested_in_peers": sum(1 for p in peers if p.flags & lt.peer_info.interesting),
                    "unchoked_by_peers": sum(1 for p in peers if not p.flags & lt.peer_info.remote_choked),
                    "peers_down_rate": sum(p.payload_down_speed for p in peers),
                    "paused": bool(s.flags & lt.torrent_flags.paused),
                    "upload_mode": bool(s.flags & lt.torrent_flags.upload_mode),
                    "save_path": s.save_path,
                })
        st = self.ses.status() if hasattr(self.ses, "status") else None
        return {
            "torrents": out,
            "listen_port": self.ses.listen_port(),
            "is_listening": self.ses.is_listening(),
            "settings": self.get_settings(),
            "disk_write_mode": self.ses.get_settings().get("disk_write_mode"),
            "download_rate_limit": self.ses.get_settings().get("download_rate_limit"),
            "events": self.events[-30:],
            "disk": self.disk(),
        }

    def disk(self) -> dict:
        u = shutil.disk_usage(self.download_dir)
        return {"total": u.total, "free": u.free, "used": u.used}

    # ---------- seed-stop ----------
    def _seed_limit_reached(self, s) -> bool:
        ratio_limit = float(self.settings.get("seed_ratio_limit", 1.0))
        time_limit = int(self.settings.get("seed_time_limit_min", 0)) * 60
        wanted = s.total_wanted or 0
        ratio = (s.all_time_upload / wanted) if wanted else 0
        if ratio_limit >= 0 and ratio >= ratio_limit:
            return True
        if time_limit > 0 and _secs(s.seeding_duration) >= time_limit:
            return True
        return False

    def _seed_limit_reached_by_id(self, tid: str) -> bool:
        try:
            return self._seed_limit_reached(self._handle(tid).status())
        except KeyError:
            return False

    def _housekeeping_loop(self):
        last_save = 0.0
        while not self._stop.wait(3):
            try:
                with self.lock:
                    changed = False
                    for h in self.ses.get_torrents():
                        s = h.status()
                        tid = _tid(s)
                        m = self.meta.setdefault(tid, {"added": time.time()})
                        if s.name and m.get("name") != s.name:
                            m["name"] = s.name
                            changed = True
                        paused = bool(s.flags & lt.torrent_flags.paused)
                        # Efter en diskfejl (fx fuld disk) går libtorrent i "upload mode" og
                        # henter ikke mere. Er der plads igen, så hent videre med det samme.
                        if s.flags & lt.torrent_flags.upload_mode and not s.is_finished \
                                and shutil.disk_usage(self.download_dir).free > 1024 ** 3:
                            h.unset_flags(lt.torrent_flags.upload_mode)
                            log.info("Henter videre efter diskfejl: %s", s.name)
                        if (s.is_seeding or s.is_finished) and not paused and not m.get("seed_override") \
                                and not m.get("awaiting_selection") and s.has_metadata:
                            if self._seed_limit_reached(s):
                                h.unset_flags(lt.torrent_flags.auto_managed)
                                h.pause()
                                m["seed_done"] = True
                                changed = True
                                h.save_resume_data()
                                log.info("Seeding stoppet: %s", s.name)
                    if changed:
                        self._save_meta()
                    if time.time() - last_save > 60:
                        last_save = time.time()
                        for h in self.ses.get_torrents():
                            if h.need_save_resume_data():
                                h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
            except Exception:
                log.exception("Fejl i housekeeping")

    # ---------- resume-data ----------
    def _load_resume(self):
        for f in sorted(self.resume_dir.glob("*.fastresume")):
            try:
                p = lt.read_resume_data(f.read_bytes())
                if not p.save_path:
                    p.save_path = str(self.download_dir)
                self.ses.add_torrent(p)
            except Exception:
                log.exception("Kunne ikke indlæse %s", f.name)

    def _alert_loop(self):
        while not self._stop.is_set():
            self.ses.wait_for_alert(1000)
            for a in self.ses.pop_alerts():
                try:
                    self._on_alert(a)
                except Exception:
                    log.exception("Fejl i alert")

    def _on_alert(self, a):
        if isinstance(a, lt.save_resume_data_alert):
            tid = str(a.params.info_hashes.get_best())
            buf = lt.write_resume_data_buf(a.params)
            tmp = self.resume_dir / f"{tid}.tmp"
            tmp.write_bytes(buf)
            tmp.replace(self.resume_dir / f"{tid}.fastresume")
        elif isinstance(a, (lt.metadata_received_alert, lt.torrent_finished_alert)):
            a.handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
        elif isinstance(a, (lt.torrent_error_alert, lt.file_error_alert,
                            getattr(lt, "listen_failed_alert", ()), getattr(lt, "tracker_error_alert", ()))):
            msg = a.message()
            log.warning("%s", msg)
            self.events.append(time.strftime("%H:%M:%S ") + msg)
            del self.events[:-50]

    def shutdown(self):
        """Gem resume-data for alle torrents og vent kort på svar."""
        self._stop.set()
        with self.lock:
            hs = [h for h in self.ses.get_torrents() if h.is_valid()]
            for h in hs:
                h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
        pending = len(hs)
        deadline = time.time() + 10
        while pending and time.time() < deadline:
            self.ses.wait_for_alert(500)
            for a in self.ses.pop_alerts():
                if isinstance(a, lt.save_resume_data_alert):
                    self._on_alert(a)
                    pending -= 1
                elif isinstance(a, lt.save_resume_data_failed_alert):
                    pending -= 1
