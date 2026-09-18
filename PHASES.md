# Investment Rebuild — Ringkasan Fase 1–7 (branch `modification`)

MiroFish awalnya adalah engine simulasi sosial multi-agent (persona media sosial, GraphRAG
berbasis Zep, simulasi OASIS). Branch `modification` merombaknya jadi engine
**analisis investasi / screening saham S&P 500**, dibangun bertahap dari data layer murni
sampai portfolio construction + Q&A agent. Dokumen ini merangkum apa yang sudah dibangun
di tiap fase, berdasarkan kode aktual di branch ini (bukan rencana/wacana).

Semua kode baru ada di `backend/app/data_layer/` (data mentah, tanpa LLM) dan
`backend/app/services/` (orkestrasi, termasuk yang pakai LLM), lalu diekspos lewat
`backend/app/api/` (Flask blueprints) dan dikonsumsi `frontend/` (Vue 3).

---

## Fase 1 — Data Layer Finansial (`backend/app/data_layer/`)

Lapisan paling dasar: ambil & cache data pasar dari yfinance, tanpa LLM, tanpa logic screening.

- **`market_data.py`** — `get_stock_context(ticker, as_of_date=None)` menggabungkan:
  - **Price** (`get_price_data`) — tidak ada lookahead, `as_of_date` benar-benar membatasi
    histori yang dibaca.
  - **Fundamental** (`get_fundamental_data`) — P/E, P/B, ROE, debt/equity, dst. **Bukan
    point-in-time** (yfinance `.info` selalu snapshot "sekarang", didokumentasikan eksplisit
    sebagai risiko lookahead-bias untuk backtest — lihat bagian Keterbatasan di bawah).
  - **Technical indicators** — SMA/RSI(14)/MACD(12/26/9)/Bollinger %B/volume-vs-avg,
    diimplementasikan manual dengan numpy (tidak pakai `ta`/`pandas_ta`/TA-Lib).
  - **Sharia compliance** (`get_sharia_compliance_data`) — screening gaya AAOIFI SS-21:
    rasio debt/market-cap dengan threshold `_SHARIA_DEBT_TO_MCAP_THRESHOLD = 0.33`, plus
    exclusion screen berbasis sektor/industri (conventional finance, alkohol, tembakau,
    judi, senjata). Bagian ini eksplisit **partial** (banyak kriteria AAOIFI lain yang
    butuh data yang yfinance tidak punya, ditandai `unavailable_checks`, tidak ditebak).
- **`cache.py`** — cache SQLite lokal (`market_cache.db`), key `(ticker, as_of_date, data_type)`,
  upsert via DELETE-then-INSERT (karena SQLite tidak men-dedupe `UNIQUE` saat kolomnya NULL).
  TTL 30 menit untuk data live, snapshot historis tidak pernah expire.
- **`universe.py`** — `screen_universe(sectors, market_cap_tiers, as_of_date)` menyaring S&P 500
  berdasarkan GICS sector + tier market cap (`classify_market_cap`: mega >200B / large 10-200B /
  mid 2-10B / small <2B). Daftar konstituen dari `get_sp500_constituents()` (scrape Wikipedia,
  cache 7 hari, fallback ke `sp500_constituents.csv` yang di-bundle kalau scrape gagal).

## Fase 2 — Entity Graph dari Data Saham (`backend/app/services/`)

Mengadaptasi pipeline entity-extraction MiroFish yang lama (awalnya untuk dokumen teks via Zep)
supaya bisa jalan dari `get_stock_context()`, tanpa LLM.

- **`financial_entity_extractor.py`** — `build_filtered_entities(stock_context) -> FilteredEntities`.
  6 tipe entity: `Company`, `Sector`, `ValuationMetric`, `FundamentalMetric`, `TechnicalSignal`,
  `ShariaScreen`. Field yang `None` di-skip + di-log, tidak pernah dikarang.
- **`seed_builder.py`** — `build_seed_from_ticker(ticker, as_of_date=None) -> FilteredEntities`,
  titik masuk "ticker → graph". Lempar `SeedBuildError` kalau price DAN fundamental sama-sama
  gagal; kalau cuma salah satu gagal, tetap kembalikan seed parsial + warning (lenient).
- **`entity_edge_builder.py`** — `build_entity_edges(seed)`, topologi bintang dari node `Company`
  ke tiap Sector/Metric/Signal/ShariaScreen. Tidak pakai LLM sama sekali (beda dari graph
  building lama yang mengandalkan Zep Cloud LLM).

## Fase 3 — Persona Investor + "Gamma" (Kepatuhan Syariah)

**`oasis_profile_generator.py`** — mengganti "1 persona netizen per entity" (lama) jadi
**tepat 6 persona investor tetap** dari satu seed graph:

- 5 arketipe investor (`INVESTOR_ARCHETYPES`): value, growth, technical, quality, macro.
  Stance (bullish/neutral/bearish) + `score` kontinu [-1, 1] dihitung **deterministik** dari
  angka riil (`_derive_investor_stance`), bukan LLM/random — LLM cuma menulis narasi di
  sekitar angka yang sudah pasti.
- 1 persona "Gamma" (`GAMMA_ARCHETYPE`) — bukan investor, tapi verdict kepatuhan syariah
  (`_derive_sharia_verdict`: compliant/non_compliant/indeterminate/unknown), juga deterministik.

## Fase 4 — Debate Room (`debate_room.py`)

`run_debate(seed, *, rounds=3, use_llm=True)` — 6 persona Fase 3 berdebat multi-round dalam
satu ruang bersama (bukan simulasi OASIS dua-platform yang lama — investigasi di fase ini
menyimpulkan OASIS tidak menambah nilai untuk 6 persona tetap tanpa social graph/feed).
Verdict Gamma adalah **invariant** sepanjang debat (dihitung sekali di awal, tidak berubah
per-round meski LLM menulis ulang kalimatnya tiap giliran).

## Fase 5 — Consensus Screening & Portfolio Optimization

- **`consensus_screener.py`** — `compute_consensus(ticker, as_of_date)` (skor konsensus dari
  5 stance investor, **tanpa panggil LLM sama sekali** — murni derivasi angka) dan
  `screen_and_rank(tickers, top_k=25)` (hard filter: ticker non-compliant syariah **hilang**
  dari hasil, bukan cuma diranking rendah).
- **`portfolio_optimizer.py`** — `build_returns_matrix()` dari harga historis riil, lalu
  `optimize_portfolio(..., model="max_sharpe"|"min_variance"|"hrp")` pakai `skfolio`
  (Ledoit-Wolf covariance shrinkage, `MeanRisk` untuk max_sharpe/min_variance,
  `HierarchicalRiskParity` untuk hrp). `build_portfolio()` merangkai
  `screen_and_rank` → `optimize_portfolio`. Skor konsensus LLM **tidak pernah** masuk ke
  matematika optimizer — cuma dipakai untuk seleksi/ranking kandidat, lalu dilaporkan
  sebagai info tambahan (diverifikasi eksplisit dengan test yang mengacak skor konsensus
  dan memastikan bobot portfolio tidak berubah).

## Fase 6 — Portfolio Agent (`portfolio_agent.py`)

`PortfolioAgent.answer(question, portfolio)` — Q&A murah (derive fakta dari portfolio dulu,
baru LLM merapikan kalimat; tidak panggil LLM sama sekali untuk kasus sederhana seperti
"ticker tidak ditemukan"). `.get_debate(ticker, portfolio, rounds=3)` — trigger debat
Fase 4 secara eksplisit untuk satu ticker dalam portfolio, tidak pernah dipanggil implisit
dari `answer()`.

## Fase 7 — API & Frontend Dashboard

- **Backend** (`backend/app/api/`, Flask blueprints — bukan FastAPI):
  - `GET /api/universe/screen` — hasil `screen_universe()` (**array JSON polos**, bukan
    object dengan field caveats).
  - `POST /api/portfolio/build`, `POST /api/portfolio/ask`, `POST /api/portfolio/debate`,
    `GET /api/portfolio/graph` (untuk preview graph satu ticker di UI).
- **Frontend** (`frontend/`, Vue 3 + Vite + d3, bukan React): satu route `/dashboard`
  (`DashboardShellView.vue`) yang membungkus 3 step (screening → build portfolio →
  workbench chat/debate) dalam satu shell yang sama, meniru shell Graph/Split/Workbench
  MiroFish yang lama (`MainView.vue`) — bukan redesign generik. Plus `/dashboard/portfolio`
  (`FinalPortfolioView.vue`) sebagai halaman laporan portfolio yang bisa di-bookmark,
  independen dari step flow. Komponen: `DashStep1Screening`, `DashStep2PortfolioBuild`,
  `DashStep3Workbench`, `DebateFeed`, `PortfolioChat`, `PortfolioResultsPanel`.
  Route/halaman OASIS lama (Home, Process, Simulation, Report, Interaction) **tidak disentuh**.

---

## Testing

`backend/tests/` — 267 test ter-collect (`pytest -q --collect-only`), 29 file test.
Test yang relevan ke fase-fase di atas (consensus_screener, debate_room,
entity_edge_builder, financial_entity_extractor, investor_persona_generator,
portfolio_agent, portfolio_api, portfolio_optimizer, seed_builder,
sharia_compliance, sharia_compliance_persona) **lulus semua**. Catatan: sebagian test ini
memanggil yfinance sungguhan (bukan offline murni).

**Fase 1 tidak punya file test sendiri** (tidak ada `test_market_data.py`/`test_cache.py`/
`test_universe.py` di repo) — ada sisa file `.pyc` untuk `test_universe` di `__pycache__`
tanpa source-nya, kemungkinan file test yang pernah ada lokal tapi belum di-commit/sudah dihapus.

## Keterbatasan yang Diketahui

1. **Fundamental data bukan point-in-time.** yfinance `.info` selalu mengembalikan snapshot
   "sekarang", bukan data historis pada `as_of_date`. Untuk backtest ke masa lalu, ini
   berarti P/E, market cap, dll yang dipakai adalah angka *hari ini*, bukan angka yang
   sungguhan berlaku di tanggal tersebut — sumber lookahead-bias yang didokumentasikan
   eksplisit di kode (`is_point_in_time: False` + `warning` di tiap response), tapi belum
   diperbaiki (butuh provider historis berbayar atau dataset snapshot).
2. **Daftar konstituen S&P 500 juga cuma snapshot "sekarang".** `get_sp500_constituents()`
   tidak punya parameter `as_of_date` — screening untuk tanggal historis tetap memakai
   keanggotaan indeks *hari ini*, bukan keanggotaan di tanggal itu (potensi survivorship
   bias). Ini didokumentasikan sebagai komentar di `universe.py`, **bukan** sebagai field
   terstruktur di response API manapun (tidak ada `universe_caveats` di kode saat ini).
3. Sharia screening di Fase 1g bersifat **partial** — sejumlah kriteria AAOIFI (rasio
   pendapatan tidak halal, investasi berbunga, dll) tidak bisa dihitung dari data yfinance
   dan secara eksplisit dilaporkan "tidak tersedia", bukan ditaksir.
