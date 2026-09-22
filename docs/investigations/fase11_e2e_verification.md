# Fase 11 — Verifikasi End-to-End SUNGGUHAN (Fase 8 → 11a)

Tanggal: 2026-09-22. Dijalankan dengan `backend/scripts/verify_e2e_pipeline.py` (dipertahankan di repo
untuk regression-check manual berikutnya — lihat §8). **Tanpa mock**: LLM sungguhan
(`Qwen/Qwen2.5-7B-Instruct-AWQ` di RunPod vLLM), Finnhub sungguhan, yfinance sungguhan, OASIS sungguhan
(2 platform, 10 round, follow-network complete graph). Tidak ada file kode diubah untuk verifikasi ini
(hanya 1 skrip baru); tidak ada commit dibuat.

**Catatan metodologi penting (baca sebelum tabel status):** verifikasi dijalankan dalam **dua pass**
dalam sesi yang sama untuk memvalidasi skrip secara bertahap sebelum membayar biaya simulasi penuh:

1. **Pass validasi** (`--skip-simulation`, 16:00:41–16:05:41 UTC): langkah 2 (persona) dan langkah 3
   (artefak+enrichment) dijalankan **fresh** (LLM sungguhan, tanpa cache). Direktori sim sementara
   (`sim_e2e_dryrun`) dihapus setelahnya — datanya diabadikan di sini karena file findings sudah dibaca
   sebelum dihapus.
2. **Pass penuh** (`sim_e2e_verify_20260922_090624`, 16:06:24–16:14:29 UTC): karena parameter
   (`as_of_date=None`, `sectors=["Energy","Communication Services"]`) **identik** dengan pass 1,
   `get_or_generate_personas` dan enrichment **cache-hit** ke run yang sama (`run_id
   af0dbfbdbe77439b8a26dc4ae3c23581`) — ini perilaku desain yang benar (persistence Fase 10/11a, lihat
   `docs/design/fase11a_oasis_adapter.md` §3), bukan cacat metodologi, tapi berarti angka waktu-tempuh
   "fresh" untuk langkah 2–3 diambil dari pass 1, sedangkan langkah 4–7 (simulasi OASIS penuh) diambil
   dari pass 2 (yang membangun `simulation_id` baru dan artefak baru dari persona run yang sama).

Direktori bukti yang dipertahankan: `backend/uploads/simulations/sim_e2e_verify_20260922_090624/`
(semua artefak, DB, log, dan `e2e_verification_findings.json` lengkap).

---

## 0. Ringkasan status

| # | Langkah | Status |
|---|---|---|
| 1 | Parameter run (universe ≥20 ticker) | **BERHASIL PENUH** — 44 ticker lolos screening |
| 2 | `get_or_generate_personas` sungguhan | **BERHASIL PENUH** |
| 3 | `build_oasis_artifacts` sungguhan (termasuk enrichment + warning propagation) | **BERHASIL PENUH** |
| 4 | `SimulationRunner.start_simulation` penuh sampai `completed` | **BERHASIL PENUH** |
| 5 | `actions.jsonl` — partisipasi & distribusi aksi | **BERHASIL DENGAN CATATAN** — lihat repetisi konten |
| 5 | Kualitas substantif konten (kepribadian vs generik) | **BERHASIL DENGAN CATATAN SERIUS** — repetisi verbatim signifikan |
| 6 | Regresi (exception, twhin-bert, `./log/`) | **BERHASIL PENUH** |
| 7 | Estimasi token/biaya | **BERHASIL DENGAN CATATAN** — instrumentasi tidak menjangkau subprocess OASIS, jadi estimasi (bukan pengukuran presisi) untuk bagian itu |

**Tidak ada kegagalan fungsional.** Satu bug regresi lama (duplikasi aksi round-0→round-1, temuan F
preflight) **terbukti sudah tidak muncul lagi** di run sungguhan ini. Catatan kualitas terbesar adalah
**repetisi konten verbatim** oleh beberapa agent di paruh kedua simulasi — bukan kegagalan pipeline,
tapi relevan untuk Fase 11b (lihat §9).

---

## 1. Parameter run (persis, untuk reproduksi)

```
as_of_date       = None (live / hari ini, 2026-09-22)
sectors          = ["Energy", "Communication Services"]
market_cap_tiers = None (tidak dibatasi)
simulation_id    = sim_e2e_verify_20260922_090624
persona run_id   = af0dbfbdbe77439b8a26dc4ae3c23581
```

`screen_universe(sectors=["Energy","Communication Services"])` memproses seluruh 503 konstituen S&P 500
(short-circuit di luar 2 sektor ini), memanggil `get_stock_context` (yfinance) untuk kandidat yang cocok
sektor, dan **meluluskan 44 ticker** (`筛选完成: 处理 503 只，跳过/失败 0 只，通过 44 只`) — jauh di atas
target ≥20. Dari 44 ticker itu, sampling berita Fase 10 (`MAX_TICKERS_PER_SECTOR=5`) memilih maksimal 10
ticker teratas by market cap untuk di-query ke Finnhub — ini keputusan desain Fase 10 yang sudah ada,
bukan sesuatu yang saya kerja-tangani di sini; dicatat supaya jelas kenapa "≥20 ticker" merujuk ke
*universe hasil screening*, bukan ke jumlah ticker yang benar-benar disampling beritanya.

---

## 2. `get_or_generate_personas` sungguhan

**Waktu tempuh (pass 1, fresh, termasuk cold-start RunPod pertama di sesi):** 291.6 detik (≈4.9 menit).
**Grounding:** `"news"` (19 artikel sample setelah dedup, ≥ `MIN_ARTICLES_FOR_GROUNDING=10`). 2 ticker
(XOM, MPC) gagal fetch berita karena timeout/connection-reset Finnhub sesaat — ditangani dengan benar
oleh `_sample_articles` (di-skip, bukan fatal; lihat log `Sampling berita: XOM dilewati`).
**Warning Fase 10:** kosong (`[]`) — tidak ada `philosophy_label` duplikat maupun nama yang menyerupai
archetype Fase 3.

8 persona yang dihasilkan (ringkas):

| # | Nama | `philosophy_label` | Tagline |
|---|---|---|---|
| 0 | Morgan 'The Momentum' McCloud | Momentum Trader | Buy what's hot, hold until it cools |
| 1 | Dana 'The Doomscrolling Diva' Drizzle | Bearish Speculator | Sell what's popular, buy what's not |
| 2 | Alex 'The Quant Wizard' Quantum | Quantitative Analyst | Numbers don't lie, they whisper |
| 3 | Elena 'The Sentiment Whisperer' Sentimentia | Sentimental Analyst | Feel the vibes, trade the trends |
| 4 | Greg 'The Risk Taker' Raging Bull | Risk Taker | High stakes, high rewards |
| 5 | Rachel 'The Fundamentalist' Reality Check | Fundamental Investor | The numbers speak, listen |
| 6 | Jordan 'The Contrarian King' Counterplay | Contrarian Investor | When everyone agrees, I bet against |
| 7 | Isaac 'The Irrational Genius' Illusionist | Behavioral Investor | The irrational mind is the ultimate trader |

**Catatan kualitas (langsung terlihat, bukan dari §5):** kedelapan nama mengikuti pola persis sama
`FirstName 'The X' LastName` — formulaic, bukan hanya philosophy_label yang berbeda tapi struktur
penamaan yang seragam di seluruh 8 persona. Ini konsisten dengan catatan kualitas 7B dari Fase 10b
(disebutkan di KONTEKS tugas) — model cenderung jatuh ke template yang sama meski instruksi eksplisit
meminta keberagaman.

---

## 3. `build_oasis_artifacts` sungguhan

**Waktu tempuh (pass 1, fresh, 1 panggilan LLM enrichment, endpoint sudah warm dari langkah 2):** 8.4 detik.

**`persona_run.warnings` di `simulation_config.json` HASIL TULISAN** (dibaca dari file, bukan return
value): `[]` — kosong, konsisten dengan tidak adanya warning dari sumber manapun (persona/enrichment/event)
di run ini. `result["warnings"] == written_config["persona_run"]["warnings"]` → `True` (diverifikasi
otomatis oleh skrip untuk kedua pass).

**Tabel enrichment (age/gender/mbti/country, 8 persona) — verifikasi manual keberagaman:**

| Nama | Age | Gender | MBTI | Country |
|---|---|---|---|---|
| Morgan 'The Momentum' McCloud | 34 | female | ENTJ | United States |
| Dana 'The Doomscrolling Diva' Drizzle | 29 | female | INTP | Canada |
| Alex 'The Quant Wizard' Quantum | 42 | male | INTP | United States |
| Elena 'The Sentiment Whisperer' Sentimentia | 37 | female | ENFP | United Kingdom |
| Greg 'The Risk Taker' Raging Bull | 30 | male | ENTP | Australia |
| Rachel 'The Fundamentalist' Reality Check | 45 | female | ISTJ | United States |
| Jordan 'The Contrarian King' Counterplay | 28 | male | INTP | United Kingdom |
| Isaac 'The Irrational Genius' Illusionist | 35 | other | INFP | Germany |

Keberagaman: **6/8 MBTI berbeda**, **5/8 negara berbeda** (2× United States, 2× United Kingdom), **3/3
gender terwakili** (female/male/other), rentang usia 28–45. **Verifikasi manual: BUKAN 8× nilai sama**
di field manapun — soft-check §3 (deteksi keseragaman) sesuai desain tidak terpicu di run ini
(`result["warnings"]` kosong mengkonfirmasi ini).

**`event_config.initial_posts`** (4 post, dari headline Finnhub sungguhan hari ini):

1. agent 4 (Greg) — *"Warren Buffett Just Stepped Down. These 2 AI Stocks Still Define Berkshire's
   Portfolio $GOOGL — via Yahoo (2026-09-22)"*
2. agent 0 (Morgan) — *"Oil Dictating All Assets Over Short-Term: Market Analysis $META — via Yahoo
   (2026-09-22)"*
3. agent 3 (Elena) — *"Columbia Global Technology Growth Fund Q2 2026 Portfolio Recap $NFLX — via
   SeekingAlpha (2026-09-22)"*
4. agent 6 (Jordan) — *"Chevron (CVX) Stock Drops Despite Market Gains: Important Facts to Note $CVX —
   via Yahoo (2026-09-21)"*

4 post dari 4 agent berbeda (bug "post dari agent sama saling menimpa", temuan C preflight, terbukti
tidak terjadi di run sungguhan ini).

---

## 4. `SimulationRunner.start_simulation` — simulasi penuh sampai `completed`

**Waktu tempuh end-to-end simulasi (pass 2):** 469.2 detik (≈7.8 menit) dari `started_at` sampai
`completed_at`. **Warm-up LLM: 2.7 detik** — endpoint RunPod sudah warm dari pass 1 (persona+enrichment
±1 menit sebelumnya), jadi run ini **tidak** mengalami cold-start 2.5–3.7 menit yang didokumentasikan di
preflight; itu tetap risiko nyata untuk run pertama setelah endpoint idle lama (lihat §7 untuk implikasi
skala).

**`run_state.json` final** (dibaca dari file, bukan hanya state in-memory):

```json
{
  "runner_status": "completed",
  "current_round": 10, "total_rounds": 10,
  "twitter_current_round": 10, "reddit_current_round": 10,
  "twitter_completed": true, "reddit_completed": true,
  "twitter_actions_count": 134, "reddit_actions_count": 140,
  "total_actions_count": 274,
  "error": null
}
```

`total_rounds = 10` ✓ (tepat sesuai desain §5: 5 jam × 30 menit/round). `error: null` ✓.

**Follow-network** (dibaca dari `follow` table SQLite kedua platform, bukan hanya log):

| Platform | Baris `follow` | `num_followers`/`num_followings` per agent |
|---|---|---|
| Twitter | 56 | **7/7 untuk SEMUA 8 agent** |
| Reddit | 56 | **7/7 untuk SEMUA 8 agent** |

56 = 8×7 (graf lengkap, tanpa self-follow) — tepat sesuai desain. Semua 8 agent punya persis 7 follower
dan 7 following di kedua platform.

**Bug regresi "aksi FOLLOW bocor ke round salah" (temuan F preflight) — VERIFIKASI: tidak muncul lagi.**
Menghitung `action_type=FOLLOW` per round dari `actions.jsonl` sungguhan: **seluruh 56 aksi FOLLOW di
kedua platform tercatat PERSIS di round 0**, tidak ada satupun di round ≥1
(`follow_actions_outside_round0: {}` untuk Twitter maupun Reddit). Perbaikan `last_rowid` yang
di-deploy sebagai bagian Fase 11a (memajukan `last_rowid` setelah round 0, §6 desain) **terbukti bekerja
di run sungguhan**, bukan hanya di test dengan config buatan tangan.

---

## 5. `actions.jsonl` — ringkasan dan penilaian kualitas

### Distribusi jenis aksi (round ≥1, tanpa FOLLOW/initial-post round 0)

| Platform | Distribusi (total termasuk round 0) |
|---|---|
| Twitter | `FOLLOW: 56` (round 0), `CREATE_POST: 50` (4 initial + 46 LLM), `QUOTE_POST: 22`, `REPOST: 3`, `LIKE_POST: 3` |
| Reddit | `FOLLOW: 56` (round 0), `CREATE_POST: 13` (4 initial + 9 LLM), `CREATE_COMMENT: 71` |

### Partisipasi agent

- **Reddit: SEMUA 8 agent aktif di SEMUA 10 round** (`rounds_with_all_8_agents_active: 10/10`) —
  partisipasi penuh sesuai desain (`activity_level=1.0`, `active_hours=24`, `time_config` dinetralkan).
- **Twitter: 7/10 round dengan 8 agent aktif** (3 round dengan agent yang menghasilkan aksi lebih sedikit
  dari 8). Catatan metodologi: metrik ini menghitung agent yang **menghasilkan aksi ter-log**, bukan
  agent yang dipilih `get_active_agents_for_round` (yang secara konfigurasi deterministik selalu memilih
  8/8 karena `agents_per_hour_min=max=8`, `activity_level=1.0`). Selisihnya konsisten dengan agent yang
  memilih `DO_NOTHING` — jenis aksi ini **tidak muncul sama sekali** di `action_type_distribution` kedua
  platform, jadi kemungkinan `do_nothing` OASIS tidak selalu tercatat ke tabel `trace` platform (di luar
  cakupan adapter Fase 11a; tidak sempat ditelusuri lebih jauh — dicatat sebagai catatan, bukan bug yang
  dikonfirmasi).

### Kutipan konten ASLI (bukan dikarang) — awal simulasi (round 0–1)

> **round 1, Greg (Risk Taker):** "Buffett stepping down? Let's focus on high-risk AI stocks with huge
> upside! 🚀🚀🚀 $GOOGL"
>
> **round 1, Isaac (Behavioral/Irrational):** "The market dances to the tune of Warren's departure, but
> true wisdom lies in the irrational. Follow the anomalies, embrace the chaos. $GOOGL #MarketAnomalies"
>
> **round 1, Dana (Bearish Speculator):** "Buffett stepping down? No surprise here. Oil still dictating
> everything. Time to sell tech stocks before they crash. #Doomscrolling #MarketCrash"

### Penilaian kualitas substantif — kesan jujur, TIDAK dipoles

**Sisi positif — stance memang mengikuti filosofi persona**, ini nyata dan terukur: Dana (Bearish) dan
Jordan (Contrarian) konsisten menulis "bet against / overvalued / sell", sementara Greg (Risk Taker) dan
Isaac (Behavioral) konsisten menulis "capitalize / ride the wave / embrace the chaos" — arah stance-nya
BERBEDA dan KONSISTEN dengan `philosophy_label` masing-masing sepanjang 10 round. Ini bukti nyata bahwa
konten tidak generik secara stance/arah pendapat.

**Sisi negatif — repetisi verbatim signifikan, lanjutan catatan kualitas 7B dari Fase 10b:** menghitung
pasangan `(agent, content)` yang identik persis di round ≥1:

| Platform | Total aksi berkonten (round ≥1) | Pasangan konten identik | Kemunculan berulang "ekstra" |
|---|---|---|---|
| Twitter | 46 | 6 template berbeda | **17** (37% dari total aksi adalah duplikat verbatim) |
| Reddit | 80 | 8 template berbeda | **47** (59% dari total aksi adalah duplikat verbatim) |

Contoh konkret: komentar Isaac di Reddit —
*"AI stocks like $GOOGL are indeed the future. Let's capitalize on these opportunities and ride the wave
of technological advancement. #TechInnovation #InvestSmart"* — muncul **verbatim identik 7 kali**, di
round **3, 5, 6, 7, 8, 9, 10** (6 dari 7 kemunculan ada di paruh kedua simulasi, round 5–10 berturutan).
Pola sama terjadi untuk Rachel, Morgan, Greg, Elena di Reddit dan Dana, Jordan, Isaac di Twitter — bukan
satu kejadian terisolasi, tapi pola sistematis di lebih dari separuh populasi agent.

**Kesimpulan kualitas:** model 7B **berhasil** mempertahankan arah stance sesuai persona sepanjang
simulasi, tapi **gagal** menghasilkan variasi kalimat baru dari waktu ke waktu — begitu sebuah agent
menemukan satu kalimat yang "cukup baik" untuk konteks (headline Buffett/GOOGL yang sama bertahan sebagai
topik dominan sepanjang 10 round karena tidak ada event baru masuk setelah round 0), ia cenderung mengulang
kalimat itu verbatim di round-round berikutnya alih-alih menulis ulang dengan kata berbeda. ~40-60% dari
seluruh konten round ≥1 adalah pengulangan verbatim. Ini **bukan kegagalan pipeline** (semua repetisi
tetap merupakan tool-call valid yang dieksekusi dan tercatat benar oleh OASIS/adapter) tapi merupakan
**catatan kualitas substantif yang relevan untuk Fase 11b**: bila sinyal Fase 11b diekstrak dari
keragaman/frekuensi opini per agent, repetisi verbatim ini akan mendistorsi sinyal tersebut (mis. "8 post
identik" seharusnya tidak dihitung sebagai 8 sinyal independen).

---

## 6. Regresi lain

- **Exception/traceback di `simulation.log` (kedua platform, 1 file gabungan):** `0` traceback, `0`
  baris mengandung `Error`/`Exception` (di luar filter `MaxTokensWarning` yang memang sengaja
  disaring skrip runner). **BERSIH.**
- **twhin-bert (Twitter recsys) dipakai sungguhan dalam alur penuh:** satu baris log normal dari
  `transformers` saat memuat model (`Some weights of BertModel were not initialized … pooler.dense...`,
  peringatan standar HuggingFace saat memuat backbone tanpa pooler fine-tuned, **bukan error**). Tidak
  ada exception dari `rec_sys_personalized_twh`. Konsisten dengan preflight item 3 (VERIFIED OK).
- **Direktori `./log/`:** **tidak ada** di `sim_dir` pada akhir run — konsisten dengan preflight item 4
  (`init_logging_for_simulation` melakukan `rmtree` pada `sim_dir/log` setelah `oasis` diimpor; handle
  file OASIS tetap berfungsi meski direktori dihapus; ini perilaku upstream yang disengaja, bukan
  masalah writability). Tidak ada error permission di log.

---

## 7. Estimasi token/biaya (order-of-magnitude, ASUMSI EKSPLISIT — lihat catatan instrumentasi)

**Catatan instrumentasi (keterbatasan diketahui):** skrip verifikasi menangkap `usage` dari OpenAI SDK
dengan mem-patch `Completions.create`/`AsyncCompletions.create` di proses Python skrip itu sendiri. Ini
menangkap penuh untuk langkah 2 (persona) dan 3 (enrichment) **karena keduanya berjalan in-process**.
**Tapi tidak menjangkau langkah 4**: `SimulationRunner.start_simulation` menjalankan `run_parallel_
simulation.py` sebagai **subprocess terpisah** (`subprocess.Popen`), sehingga setiap panggilan LLM OASIS
(setiap keputusan agent) terjadi di proses lain yang tidak ter-monkeypatch. Angka token OASIS di bawah
karena itu adalah **estimasi**, bukan pengukuran, dengan asumsi dinyatakan eksplisit.

**Persona generation (diukur presisi dari prompt/response yang benar-benar tersimpan, bukan tebakan):**

- Prompt: 4.048 karakter → ≈1.012 token (asumsi ~4 karakter/token bahasa Inggris)
- Completion: 5.620 karakter (8 persona penuh) → ≈1.405 token
- **≈2.417 token, 1 panggilan**

**Enrichment (diukur presisi sama):**

- Prompt: 6.079 karakter → ≈1.519 token
- Completion: 1.705 karakter (8 profil demografis) → ≈426 token
- **≈1.945 token, 1 panggilan**

**Simulasi OASIS (ESTIMASI, asumsi eksplisit):** total aksi ber-LLM (round ≥1, exclude FOLLOW/initial
post yang murni `ManualAction` tanpa panggilan LLM) = 46 (Twitter) + 80 (Reddit) = **≈155 panggilan LLM**.
Asumsi per panggilan (diselaraskan dari satu titik data empiris preflight — "~1020 prompt / ~40
completion token" untuk skenario 3-agent minim-konteks — dilebarkan mengingat run ini 8 agent dengan
feed/follow-network lebih ramai): **≈1.200 prompt token + ≈80 completion token per panggilan**.

- **≈155 × 1.200 = 186.000 token prompt, 155 × 80 = 12.400 token completion**
- **≈198.400 token (estimasi), 155 panggilan**

**Total 1 run penuh (8 agent, 10 round, 2 platform):** ≈2.417 + 1.945 + 198.400 ≈ **≈202.800 token**
(sebagian besar — >97% — berasal dari estimasi OASIS, bukan pengukuran persis).

**Waktu 1 run penuh (bila dijalankan sekali dari nol, tanpa cache-hit seperti pass 2 di atas):**
persona (291,6s) + artefak (8,4s) + simulasi (469,2s) ≈ **769 detik ≈ 12,8 menit**, TANPA cold-start
tambahan di tengah jalan (asumsi endpoint tetap warm sepanjang 1 run; cold-start 2,5–3,7 menit per
preflight bisa menambah waktu bila endpoint sempat idle & scale-to-zero).

### Ekstrapolasi kasar ke backtest 252 hari trading (asumsi eksplisit, BUKAN presisi)

Asumsi: 1 as_of_date = 1 run independen (universe/persona/enrichment berbeda per hari → **tidak ada
cache-hit lintas hari**, tidak seperti pass 2 di atas); skala tetap 8 agent/10 round/2 platform;
dijalankan **berurutan** (tanpa paralelisasi lintas hari):

- **Token:** 252 × ≈202.800 ≈ **≈51 juta token** untuk satu backtest 1 tahun pada skala kecil ini.
- **Waktu (berurutan, tanpa cold-start berulang):** 252 × 769s ≈ 193.788s ≈ **≈53,8 jam (≈2,2 hari)**
  wall-clock murni komputasi.
- **Waktu (berurutan, DENGAN asumsi pesimis cold-start ~3 menit ekstra di SETIAP hari** — bila endpoint
  RunPod scale-to-zero di antara hari-hari backtest, yang plausible untuk serverless**):** tambah
  252 × 180s ≈ 45.360s ≈ 12,6 jam → total **≈66,4 jam (≈2,8 hari)**.
- **Lever paling jelas untuk skala lebih besar:** paralelisasi lintas as_of_date (RunPod serverless bisa
  auto-scale concurrent worker), yang berpotensi memangkas waktu wall-clock jauh di bawah 2–3 hari
  meski total token/biaya tetap sama. Ini tidak diverifikasi di sini — murni proyeksi.

---

## 8. Skrip verifikasi yang dipertahankan

`backend/scripts/verify_e2e_pipeline.py` — dipertahankan (bukan file sekali-pakai) untuk regression-check
manual berikutnya. Cara pakai:

```bash
cd backend
python scripts/verify_e2e_pipeline.py \
  --sim-id <opsional> \
  --sectors "Energy,Communication Services" \
  --as-of-date <opsional, kosong=live> \
  --max-wait-seconds 3600 \
  [--skip-simulation]   # hanya langkah 2-3, murah, untuk validasi cepat
```

Menulis `<sim_dir>/e2e_verification_findings.json` berisi semua temuan terstruktur (timing, warning,
tabel enrichment, follow-network, distribusi aksi, contoh konten, log token usage) untuk dibaca ulang.
**Keterbatasan yang perlu diketahui pemakai berikutnya:** penangkapan token usage TIDAK menjangkau
subprocess OASIS (lihat §7) — untuk pengukuran presisi biaya OASIS, instrumentasi perlu dipindah ke
dalam `run_parallel_simulation.py` sendiri atau ke level `camel.ChatAgent`, di luar cakupan verifikasi
ini (skrip ini murni untuk verifikasi, bukan implementasi baru).

---

## 9. Kesimpulan akhir

**Pipeline Fase 8–11a SIAP dipakai sebagai fondasi Fase 11b**, dengan satu catatan kualitas yang perlu
diperhitungkan saat mendesain ekstraksi sinyal:

- **Integrasi teknis penuh terbukti bekerja end-to-end sungguhan**: persona generation → enrichment →
  4 artefak OASIS → simulasi 2 platform 10 round dengan follow-network lengkap → `run_state.json`
  `completed` dengan `error: null`. Tidak ada exception, tidak ada crash, tidak ada artefak yang gagal
  ditulis.
- **Kedua bug regresi yang didokumentasikan di preflight (duplikasi aksi round-0→1, post initial saling
  menimpa) terbukti sudah diperbaiki dan TIDAK muncul lagi di run sungguhan** — bukan hanya lolos test
  dengan config buatan tangan, tapi juga lolos di run nyata dengan LLM sungguhan.
- **Follow-network bekerja sempurna** (56/56 edge, 7/7 follower+following semua agent, kedua platform).
- **Warning propagation (perbaikan hari ini) terverifikasi tidak meregresi**: `persona_run.warnings`
  konsisten antara `result` dan file di kedua pass, termasuk saat kosong.
- **Catatan yang perlu diperhitungkan untuk Fase 11b, bukan pemblokir Fase 11a:** repetisi konten
  verbatim signifikan (37–59% dari aksi berkonten round ≥1) dari model 7B setelah beberapa round tanpa
  event baru masuk ke simulasi. Bila Fase 11b mengekstrak sinyal dari frekuensi/keragaman opini per
  agent, perlu mekanisme dedup-per-agent (atau event/news baru yang disuntikkan di tengah simulasi,
  bukan hanya di round 0) supaya repetisi verbatim tidak disalahartikan sebagai sinyal keyakinan yang
  kuat.
- **Estimasi biaya/waktu** (§7) menunjukkan skala 8-agent/10-round/2-platform per hari trading masih
  terjangkau (~13 menit, ~203K token per hari), tapi backtest 252 hari berurutan akan memakan ~2-3 hari
  wall-clock dan ~51 juta token — layak dipertimbangkan paralelisasi lintas hari sebelum backtest skala
  penuh dimulai.

Tidak ada implementasi Fase 11b dimulai. Menunggu review dokumen ini.

---

## Lampiran — Konfirmasi test suite (di luar deliverable utama)

Total suite backend (`cd backend && python -m pytest -q`): **455 passed, 0 failed** (455 item
dikumpulkan, 455 lulus; 308,4 detik). Rincian yang relevan dengan pekerjaan warning-propagation kemarin:
`tests/test_persona_oasis_adapter.py` = **84/84 passed** (bagian dari total 455, bukan tambahan
terpisah). Tidak ada regresi di seluruh suite akibat perubahan `persona_oasis_adapter.py`.
