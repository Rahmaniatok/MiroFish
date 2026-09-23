# Zep vs. sampling Fase 10 pada skala 44 ticker — perbandingan apples-to-apples

Investigasi lanjutan dari [zep_viability_check.md](zep_viability_check.md), yang membuktikan Zep hidup tapi salah membandingkannya dengan Fase 2 (data fundamental — tidak ada yang mengusulkan itu). Pertanyaan yang benar-benar relevan: **apakah Zep GraphRAG dari SELURUH 44 ticker universe (Energy + Communication Services) adalah cara yang lebih baik untuk grounding Fase 10 (persona) dan sumber kandidat `initial_posts` Fase 11a, dibanding sampling manual 10-ticker yang sekarang?**

Investigasi murni — **tidak ada kode produksi diubah**. Dua skrip sekali-pakai (tidak di-commit) menjalankan kode produksi asli tanpa modifikasi (`screen_universe`, `get_news_data`, `GraphBuilderService`, `ZepEntityReader`, dan fungsi privat `persona_generator._select_tickers`/`_sample_articles` untuk mengambil angka pendekatan-sekarang secara presisi, bukan dikutip dari dokumen lain). Data 100% nyata: Finnhub live, Zep Cloud sungguhan, universe hasil `screen_universe(sectors=["Energy","Communication Services"])` pada 2026-09-22 (44 ticker lolos, sama seperti run E2E sebelumnya).

## Ringkasan jawaban

| # | Pertanyaan | Jawaban terukur |
|---|---|---|
| 1 | Waktu batch 44-ticker-scale | **609,7 detik (≈10,2 menit)** untuk 87 item/26.520 byte — diukur langsung, bukan dikutip |
| 2 | Kualitas ekstraksi vs. sampling sekarang | **Jauh lebih kaya secara struktur** (169 node, 212 edge fakta granular) — tapi Fase 10b saat ini secara desain **tidak memakai/tidak butuh** fakta terstruktur |
| 3 | Bisa pilih kandidat `initial_posts` representatif dari 44 ticker? | **Ya, terbukti: 36/36 ticker yang dikirim (82% dari 44) berhasil dipetakan ke entity nyata** — vs. **9/44 (20%) yang pernah tersentuh sampling sekarang** |
| 4 | Biaya nyata | **112 Zep credit** untuk 1 hari (~$0,28 di tier Flex; gratis di free tier 10.000 credit/bulan) — **murah**. Biaya sungguhan ada di **waktu**, bukan uang |
| 5 | Realistis mengganti pipeline dengan Zep? | **Bukan solusi yang tepat UNTUK MASALAH INI SECARA SPESIFIK** — akar masalah representativitas ada di cap sampling (`MAX_TICKERS_PER_SECTOR=5`), yang bisa diperbaiki TANPA Zep dengan mengubah 1 baris. Zep punya nilai tambah nyata tapi berbeda (lihat §5) |

---

## 1. Skala & waktu — angka nyata, diukur langsung

**Universe:** `screen_universe(sectors=["Energy","Communication Services"])` → 44 ticker lolos (konsisten dengan run E2E sebelumnya).

**Fetch berita SEMUA 44 ticker** (`get_news_data`, live, tanpa cap artikel — sesuai desain Fase 8c "TIDAK ADA cap jumlah artikel"): 301,6 detik, **37/44 sukses**, **7/44 gagal karena Finnhub read-timeout** (`GOOGL, TMUS, WBD, FANG, LYV, ECHO, EXE` — bukan "tidak ada berita", tapi timeout jaringan; `fetch_news_data` **tidak auto-retry** kegagalan jaringan, jadi ini kegagalan permanen untuk run itu). **0/44 ticker punya nol artikel** — total 7.344 artikel mentah di 37 ticker yang sukses (rata-rata ~198/ticker, jauh di atas kebutuhan). Temuan sampingan yang relevan di luar Zep: **keandalan fetch Finnhub sendiri sudah jadi masalah di skala 44 ticker** (16% gagal), terlepas dari Zep dipakai atau tidak.

**Sample dikirim ke Zep** (meniru cap per-ticker Fase 10 yang sekarang — 3 artikel terbaru/ticker, dedup by `article_id` — supaya perbandingan apples-to-apples soal *volume per ticker*, bedanya cuma jumlah ticker yang di-cover): **87 item, 36 ticker berbeda** (36, bukan 37 — 1 ticker, `NWSA`, seluruh top-3 artikelnya duplikat `article_id` dengan `NWS`, dua kelas saham perusahaan yang sama; dedup lintas-ticker yang sudah ada di kode produksi men-drop-nya sepenuhnya, bukan bug baru, tapi konsekuensi nyata dari memperluas ke lebih banyak ticker).

**Batch Zep Cloud sungguhan** (`GraphBuilderService.add_text_batches` + `_wait_for_batch`, kode produksi, tidak dimodifikasi):

```
batch_submitted:  87 item, submit 1,2 detik
batch_completed:  status=succeeded, wait 608,6 detik, total ingest 609,7 detik (≈10,2 menit)
```

**Ini angka pengukuran langsung untuk skala ini, bukan kutipan dari skrip lain.** Investigasi sebelumnya menyebut skrip validasi existing pakai timeout 900 detik — timeout itu memang cukup (609,7s < 900s), tapi skenarionya BEDA skala (skrip itu ~14 episode kecil, bukan 87). Untuk 44-ticker-scale yang sesungguhnya, **≈10 menit** adalah angka yang benar untuk dipakai dalam estimasi biaya-waktu, bukan 900 detik.

Baca balik entity (`ZepEntityReader`, kode produksi): 5,0 detik untuk 169 node + 212 edge. Baca balik BUKAN bottleneck; ingest-nya yang mahal.

---

## 2. Kualitas ekstraksi — lebih kaya, tapi kekayaan itu tidak dipakai desain sekarang

Dari 87 episode (headline+summary, ontology kecil `Company`/`Person`/`MarketEvent`), Zep mengekstrak **169 node total, 212 edge, 112 di antaranya bertipe (Company/Person/MarketEvent) setelah filter**. Ini fakta granular sungguhan, bukan node generik kosong — contoh nyata dari hasil:

- `EOG Resources`: "turun 2,1% minggu lalu, turun 3,5% bulan lalu, return YTD 34,46%, total shareholder return 5 tahun 133,86%" — angka presisi dari teks.
- `TKO Group Holdings`: `Bernstein` menaikkan target harga ke $240 dari $235 — entity `Person` (analis) terhubung ke `Company` lewat edge `COMMENTS_ON`.
- `Devon Energy` ↔ `MPLX LP`: edge `LIQUIDATED_SHARES_OF`/joint-venture — relasi antar-perusahaan yang tidak ada di representasi teks datar.
- `TRGP`: TD Cowen mengidentifikasi kontrak 20-tahun dengan ExxonMobil sebagai katalis utama pertumbuhan Targa Resources.

**Dibandingkan dengan sampling manual (19 artikel/8 persona dari run E2E sebelumnya):** perbandingan yang adil bukan "19 artikel vs 169 node", tapi **apa yang sebenarnya DIKONSUMSI pipeline dari kedua sumber itu**:

- Pipeline sekarang (`persona_generator._format_article_line`) mengubah tiap artikel jadi SATU baris teks datar `[tanggal] TICKER | publisher | headline | summary(≤200 char)`, dikirim ke LLM dengan instruksi eksplisit: **"gunakan hanya sebagai inspirasi mood, JANGAN dianggap fakta yang perlu diverifikasi"** (`_NEWS_INSTRUCTION`, `persona_generator.py`). Fase 10b secara desain **sengaja tidak butuh fakta granular** — tujuannya delapan kepribadian investor fiksi yang berbeda-beda, bukan analisis fundamental.
- Zep memberi struktur (entity + relasi + atribut presisi) yang **tidak dipakai** oleh desain prompt Fase 10b saat ini. Kekayaan ekstraksi Zep OBJEKTIF LEBIH TINGGI, tapi itu kelebihan kapasitas yang tidak ada konsumennya di Fase 10b tanpa perubahan desain prompt (mis. mengubah dari "loose inspiration" jadi "grounded facts" — itu keputusan desain, bukan keterbatasan teknis Zep).

**Verdict §2: LEBIH BAIK secara kekayaan struktural — tapi "lebih baik" itu untuk kebutuhan yang belum ada di Fase 10b sekarang.**

---

## 3. Kandidat `initial_posts` representatif — bukti konkret

Ini pertanyaan dengan jawaban paling langsung dan paling penting.

**Cakupan ticker, apples-to-apples pada universe 44-ticker yang SAMA:**

| Pendekatan | Ticker dipertimbangkan | Ticker yang benar-benar dapat artikel | % dari 44 |
|---|---|---|---|
| **Fase 10 sekarang** (`_select_tickers`, `MAX_TICKERS=20`, cap 5/sektor) | 10 | 9 | **20,5%** |
| **Kirim ke Zep, semua 44 ticker** | 37 (yang fetch-nya sukses) | **36 dipetakan ke entity nyata** | **81,8%** |

Daftar 10 ticker yang PERNAH dipertimbangkan sampling sekarang: `GOOGL, GOOG, META, XOM, CVX, NFLX, VZ, COP, VLO, MPC` — **semuanya mega-cap** (raksasa Energy + raksasa Communication Services). **Tidak ada satu pun ticker mid/small-cap** yang pernah punya kesempatan jadi kandidat `initial_posts`, karena `build_event_config` (Fase 11a) hanya memilih dari `sample_articles` yang SAMA yang sudah dibatasi 10 ticker itu sejak awal (`persona_oasis_adapter.py` — bukan langkah terpisah yang bisa diperbaiki sendiri; ia mewarisi keterbatasan sampling Fase 10b).

Ticker yang MUNCUL di graph Zep tapi TIDAK PERNAH bisa muncul di sampling sekarang (karena bukan di 10 besar market cap): `TKO Group Holdings`, `EQT Corporation`, `Texas Pacific Land Corporation`, `AppLovin`, `Take-Two Interactive`, `Omnicom`, `Fox Corporation` (A & B), `Charter Communications`, `Halliburton`, `Kinder Morgan`, `Oneok`, `Williams Companies`, `Baker Hughes`, `Devon Energy`, `Reddit`, `APA Corporation`, `Occidental Petroleum`, dan lainnya — **17 ticker mid/small-cap tambahan** yang punya entity/fakta terekstrak dengan baik (lihat §2 untuk contoh detail per ticker), bukan cuma node kosong.

**Jawab langsung pertanyaan 3:** dari 44 ticker, **36 (82%) punya entity/event terekstrak dengan baik — bukan cuma 4-10 yang "beruntung"**. Ini bukti konkret, bukan proyeksi.

**TAPI — nuansa penting untuk jawaban 5:** graph Zep punya cakupan lebih luas KARENA ia diberi input dari 44 ticker (bukan 10). `build_event_config` sendiri (logika pemilihan: sort by recency, 1 artikel/ticker, verbatim, tanpa LLM) **sudah bisa langsung menghasilkan cakupan yang sama luasnya TANPA Zep sama sekali**, cukup dengan memberi `sample_articles` dari ke-44 ticker (bukan Zep) ke fungsi yang sudah ada. Zep tidak menambah *jumlah ticker yang bisa dijangkau* — itu murni fungsi dari berapa banyak ticker yang di-*fetch* beritanya, yang dibatasi oleh `_select_tickers`/`MAX_TICKERS_PER_SECTOR=5`, sebuah angka di `persona_generator.py`, bukan keterbatasan Zep atau non-Zep.

Nilai tambah Zep yang SEBENARNYA unik di sini (bukan sekadar cakupan) adalah kemampuan memilih kandidat lewat **sinyal graph** — mis. entity dengan degree/edge terbanyak (`TRGP` muncul di 6 edge dari berbagai sudut: analyst rating, kontrak ExxonMobil, dst.) sebagai proxy "paling banyak dibicarakan hari ini", bukan sekadar "paling baru". Itu sinyal yang **tidak bisa didapat dari sort-by-recency biasa** — tapi juga sinyal yang **belum diminta oleh desain `build_event_config` sekarang** (yang murni recency-based, sengaja "tanpa LLM").

---

## 4. Biaya nyata

**Panggilan API Zep** untuk 1 hari/44-ticker-scale (87 item): 1× `graph.create`, 1× `set_ontology`, 1× `batch.create`, 1× `batch.add` (87 item muat dalam satu panggilan, di bawah limit 350/call), 1× `batch.process`, ~203× `batch.get` (polling tiap 3 detik selama 609,7 detik — **ini yang paling sering**, tapi `batch.get` termasuk operasi baca yang menurut halaman pricing Zep **0 credit**), lalu `graph.node.get_by_graph_id`/`graph.edge.get_by_graph_id` (paging, beberapa panggilan, juga retrieval = 0 credit).

**Biaya yang benar-benar ditagih** (dari halaman pricing resmi Zep, getzep.com/pricing, diambil langsung — bukan ditebak):
- Ditagih **per Episode**: **1 credit/350 byte pertama, +1 credit per 350 byte tambahan (atau bagian daripadanya)**. Retrieval/storage/graph storage = **0 credit**.
- Free tier: **10.000 credit/bulan**. Flex: $125/bulan untuk 50.000 credit (overage $25/10.000 credit).

**Angka nyata untuk run ini:** 87 episode, 26.520 byte total → **112 credit** (dihitung persis dari byte tiap episode, bukan dari total byte dibagi 350 — karena tiap episode punya lantai minimum 1 credit, 87 episode pendek menghasilkan overhead lebih besar dari total-byte/350 mentah). **112 credit ≈ 1,1% dari kuota free tier bulanan** untuk SATU hari 44-ticker. Di tier Flex ($125/50.000 credit), itu setara **≈$0,28/hari**.

**Untuk backtest 252 hari trading** (asumsi 1 as_of_date = 1 batch independen, mengikuti asumsi run E2E sebelumnya): 252 × 112 ≈ **28.224 credit total**. Itu **di bawah free tier TAHUNAN** (10.000×12=120.000) kalau tersebar sepanjang tahun, dan hanya **≈$70 overage** di tier Flex kalau dipadatkan dalam waktu sebulan. **Biaya credit Zep BUKAN faktor pembatas.**

**Biaya sesungguhnya ada di WAKTU, bukan uang:** ≈10,2 menit tambahan PER as_of_date, murni menunggu batch Zep `succeeded` — ditambahkan SERIAL ke waktu pipeline yang sudah ada (≈13 menit/hari menurut run E2E sebelumnya, persona+artefak+simulasi). Itu **≈77% penambahan waktu harian**. Untuk backtest 252 hari yang sebelumnya diestimasi ≈53,8–66,4 jam wall-clock: tambahan 252 × 610 detik ≈ **42,7 jam (≈1,8 hari) lagi**, kalau dijalankan serial seperti asumsi run sebelumnya. Ini bisa diparalelkan (sama seperti opsi paralelisasi OASIS yang sudah dicatat run E2E sebelumnya), tapi sebagai biaya tambahan yang perlu diperhitungkan, ini nyata dan besar — **bukan Finnhub yang gratis** seperti pendekatan sampling sekarang.

**Perbandingan langsung:**

| | Sampling manual (sekarang) | Zep (44-ticker) |
|---|---|---|
| Biaya uang | Gratis (Finnhub free tier) | ≈$0,28/hari (murah, atau gratis di free tier Zep) |
| Biaya waktu/hari | ~0 detik tambahan (fetch+sort, in-process) | **+~10,2 menit** (tunggu batch async) |
| Cakupan ticker | 9/44 (20%), selalu mega-cap | 36/44 (82%), lintas market-cap |
| Kompleksitas operasional | Rendah (fungsi murni, sudah ada) | Sedang (ontology, batch polling, retry policy, cross-ticker dedup) |

---

## 5. Kesimpulan langsung

**TIDAK, mengganti seluruh pipeline grounding Fase 10 + sumber `initial_posts` Fase 11a dengan Zep GraphRAG bukan langkah yang tepat SEKARANG — bukan karena Zep tidak layak (ia layak, terbukti §1–4), tapi karena Zep bukan solusi untuk masalah yang sebenarnya diajukan.**

Alasan konkret, bukan dikutip dari konteks lain:

1. **Akar masalah representativitas `initial_posts` bukan di ekstraksi entity, tapi di cap sampling ticker.** `_select_tickers` (`persona_generator.py`) membatasi ke `MAX_TICKERS=20` dengan `MAX_TICKERS_PER_SECTOR=5` — dengan cuma 2 sektor terpilih, itu efektif jadi 10 ticker, semuanya mega-cap karena universe diurutkan market-cap turun. `build_event_config` (Fase 11a) 100% mewarisi keterbatasan ini karena `sample_articles`-nya persis sama dengan yang dipakai Fase 10b. **Menaikkan/menghapus cap itu (perubahan parameter, bukan Zep) sudah cukup untuk membuat `initial_posts` mencakup 36+/44 ticker** — persis manfaat yang diinginkan di §3, tanpa Zep, tanpa +10 menit/hari, tanpa biaya credit.
2. **Kekayaan struktural Zep (§2) nyata lebih tinggi, tapi Fase 10b saat ini secara desain sengaja tidak memakai fakta granular** ("gunakan hanya sebagai inspirasi mood... jangan dianggap fakta yang perlu diverifikasi"). Memakai output Zep secara penuh butuh mendesain ulang prompt Fase 10b dari "loose inspiration" jadi "grounded synthesis" — itu perubahan desain produk, bukan cuma pertukaran sumber data.
3. **Nilai tambah unik Zep yang sebenarnya** — pemilihan kandidat lewat sinyal graph (entity paling terhubung/dibicarakan, bukan sekadar artikel terbaru) — adalah kemampuan nyata yang TIDAK bisa didapat dari pendekatan sampling manual manapun. Tapi ini juga bukan kebutuhan yang diminta desain `build_event_config` sekarang (sengaja recency-based, sengaja tanpa LLM/graph).
4. **Biaya nyata condong ke waktu, bukan uang** (§4): +~10 menit/hari, +~1,8 hari untuk backtest 252-hari penuh. Untuk manfaat yang sepenuhnya bisa didapat dengan perubahan 1 parameter (poin 1), biaya waktu ini tidak sepadan SEKARANG.

**Rekomendasi bertingkat:**
- **Langkah murah dan langsung untuk masalah yang diajukan:** naikkan/hapus `MAX_TICKERS_PER_SECTOR` di `persona_generator.py` supaya sampling (dan karenanya `initial_posts`) mencakup lebih banyak ticker di luar mega-cap — ini menyelesaikan keluhan representativitas §3 secara langsung, terverifikasi cakupannya bisa sampai 36+/44, tanpa Zep sama sekali.
- **Zep tetap kandidat yang layak untuk fase berikutnya** JIKA arah desain Fase 10/11 berubah untuk benar-benar menginginkan sinyal graph (mis. "pilih initial post dari entity paling sentral hari ini" atau "grounding persona pada fakta nyata, bukan mood") — bukan sebagai pengganti sampling, tapi sebagai lapisan tambahan di atas sampling yang sudah diperluas. Keputusan itu di luar cakupan investigasi ini (murni investigasi, bukan implementasi).

---

## Lampiran — parameter run, untuk reproduksi

```
sectors            = ["Energy", "Communication Services"]
as_of_date         = None (live, 2026-09-22)
universe screened  = 44 ticker
news fetch         = get_news_data(ticker, as_of_date=None) untuk semua 44, live Finnhub
                      (37 sukses, 7 gagal karena network timeout, 0 dengan nol artikel)
articles per ticker ke Zep = 3 (top recency, dedup article_id) — sama seperti cap Fase 10 sekarang
Zep graph_id (dihapus setelah investigasi) = mirofish_investigation44_1790081460
Zep batch_id       = 5b01efee-7068-42cd-af61-ce722e15f6ac
ontology           = Company(ticker) / Person / MarketEvent(event_type);
                      edges INVOLVES(MarketEvent→Company), COMMENTS_ON(Person→Company)
```

Skrip investigasi (sekali-pakai, tidak di-commit, tersimpan di scratchpad sesi) memanggil `GraphBuilderService`, `ZepEntityReader`, `screen_universe`, `get_news_data`, dan `persona_generator._select_tickers`/`_sample_articles` PERSIS seperti produksi, tanpa modifikasi. Graph Zep uji dihapus di blok `finally` setelah pembacaan selesai — tidak ada sampah tertinggal di project Zep.
