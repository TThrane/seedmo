# seedmo

Torrent- og magnet-downloader, der kører på din QNAP NAS og styres fra browseren på Mac og iPhone. Den henter videre, selv om Mac'en er slukket, og lægger filerne direkte i en delt mappe på NAS'en.

- Magnet-links og .torrent-filer (indsæt, træk og slip eller vælg fil)
- Vælg hvilke filer i en torrent der skal hentes, også før der hentes noget
- Hastighedsgrænser for download og upload
- Stopper selv med at seede ved en ratio eller efter en bestemt tid
- Husker alt ved genstart
- Login med adgangskode
- ThraneUI: Signal, Papir, Instrument og Moderne, i lys og mørk

> Brug kun seedmo til indhold, du har ret til at hente og dele, fx Linux-images, åbne film og egne filer.

---

## Opsætning på QNAP

Det tager ca. 10 minutter. Container Station skal være installeret (App Center → Container Station).

### 1. Læg mappen på NAS'en
Kopiér hele `seedmo`-mappen til NAS'en, fx til `Container/seedmo/app`. Det kan du gøre i Finder.

Opret også de to mapper, som seedmo skal bruge:
- `Container/seedmo/config`: indstillinger og huskede torrents
- `Downloads/seedmo`: her lander de hentede filer

### 2. Ret adgangskoden
Åbn `docker-compose.yml` og skift `skift-mig` ud med din egen adgangskode.

Hvis dine mapper hedder noget andet end ovenfor, skal du også rette de to linjer under `volumes:`.

### 3. Start seedmo
Åbn **Terminal** på Mac'en, og log ind på NAS'en (brug NAS'ens IP-adresse):

```
ssh admin@192.168.1.10
```

Kør derefter:

```
cd /share/Container/seedmo/app
docker compose up -d --build
```

Første gang tager det et par minutter.

### 4. Åbn seedmo
Gå til `http://nas-ts864.local:8095` (eller NAS'ens IP-adresse) i browseren, og log ind med din adgangskode.

På iPhone kan du åbne samme adresse i Safari og vælge **Share → Add to Home Screen**.

### 5. Hurtigere downloads (valgfrit)
Åbn port **51515** (både TCP og UDP) i din router, og send den videre til NAS'en. seedmo virker også uden, men så kan færre peers forbinde til dig.

### Opdatering
Kopiér den nye `app`-mappe ind over den gamle, og kør `docker compose up -d --build` igen. Dine torrents og indstillinger bevares.

### Alternativ: uden SSH
Læg projektet i et GitHub-repo. Så bygger GitHub Actions (`.github/workflows/docker.yml`) automatisk billedet `ghcr.io/tthrane/seedmo:latest`. I Container Station opretter du derefter en app og erstatter `build: .` med:

```
image: ghcr.io/tthrane/seedmo:latest
```

Er repoet privat, skal billedet også gøres offentligt under **Packages** på GitHub, eller Container Station skal logge ind på ghcr.io.

---

## Brugervejledning

### Tilføj en torrent
- **Magnet-link:** sæt linket ind i feltet øverst, og tryk **Tilføj**. Du kan også bare trykke ⌘V et sted på siden.
- **Link til en .torrent-fil:** sæt linket ind i samme felt.
- **.torrent-fil:** træk filen ind i vinduet, eller tryk **+ .torrent-fil**.

### Status
Hver torrent har en bjælke med fire trin:

| Trin | Betyder |
|---|---|
| Metadata | Henter oplysninger om filerne (kun ved magnet-links) |
| Henter | Filerne hentes |
| Seeder | Færdig, og du deler med andre |
| Færdig | Seeding er stoppet. Filerne ligger klar |

### Vælg filer
Tryk på en torrent for at se filerne. Fjern fluebenet ved de filer, du ikke vil have. Det gemmes med det samme.

Vil du vælge filer, før der hentes noget, så slå **Vælg filer, før der hentes** til under **Indstillinger**. Nye torrents står så som **Vælg filer**, indtil du har sat flueben.

### Pause, start og fjern
- **Pause** stopper en torrent. **Start** sætter den i gang igen.
- **Seed igen** deler en færdig torrent igen, uden grænse, indtil du sætter den på pause.
- **Fjern** fjerner torrenten fra listen. Sæt flueben i **Slet også de hentede filer**, hvis filerne også skal slettes.

### Indstillinger
| Indstilling | Forklaring |
|---|---|
| Download-/upload-grænse | I KB/s. 0 betyder ubegrænset. Eksempel: 5000 ≈ 5 MB/s |
| Begræns også trafik på eget netværk | Normalt gælder grænserne kun trafik til og fra internettet |
| Aktive downloads | Hvor mange torrents der hentes samtidig. Resten venter i kø |
| Stop ved ratio | 1 betyder, at du deler lige så meget, som du har hentet. 0 stopper med det samme. −1 betyder aldrig |
| Stop efter tid | Antal minutter, der højst skal seedes. 0 betyder ingen tidsgrænse |
| Tema / Lys eller mørk | Gemmes i den browser, du bruger |

### To-trins-login
Gå til **Indstillinger → To-trins-login → Slå til**. Scan QR-koden med 1Password, Google Authenticator eller en anden godkendelsesapp, og skriv den 6-cifrede kode. Derefter skal du bruge både adgangskode og kode, når du logger ind.

Efter 5 forkerte forsøg bliver den pågældende adresse låst ude i 15 minutter.

Mister du adgangen til appen, kan du slette filen `Container/seedmo/config/totp.json` i File Station og genstarte seedmo. Så er to-trins-login slået fra igen.

### Adgang udefra (https://seedmo.thrane.dev)
`qnap.yml` indeholder også **Caddy**, som giver seedmo en https-adresse og selv henter og fornyer certifikatet.
- DNS: `seedmo.thrane.dev` er en CNAME til `thranets864.myqnapcloud.com` (hos Simply).
- Routeren skal sende port **443** videre til NAS'ens port **9443** og port **80** til **9080**.

Slå to-trins-login til, før du åbner for adgang udefra.

### Magnet-links direkte fra browseren
Under **Indstillinger** kan du trykke **Brug seedmo til magnet-links**. Så åbner magnet-links på andre sider direkte i seedmo.

Det virker kun i Chrome og Firefox og kun, når seedmo åbnes via **https**, fx gennem QNAP's reverse proxy med et certifikat. Safari understøtter det ikke. Uden https kan du stadig kopiere linket og trykke ⌘V i seedmo.

---

## Teknik

- Python 3.12, FastAPI og libtorrent 2.1 i én Docker-container
- Web-UI i ren HTML, CSS og JS. Skrifterne ligger lokalt, så intet hentes fra nettet
- `/config` indeholder `settings.json`, `meta.json`, `secret.key` og `resume/` med én `.fastresume` pr. torrent
- Miljøvariabler: `SEEDMO_PASSWORD`, `SEEDMO_PEER_PORT` (51515 i opsætningen, 6881 som standard), `SEEDMO_CONFIG`, `SEEDMO_DOWNLOADS`
- Billedet er bygget til Intel/AMD (linux/amd64)

### Lokal udvikling
```
pip install -r requirements.txt
SEEDMO_CONFIG=./data/config SEEDMO_DOWNLOADS=./data/downloads SEEDMO_PASSWORD=test \
  python -m uvicorn app.main:app --port 8080
```
