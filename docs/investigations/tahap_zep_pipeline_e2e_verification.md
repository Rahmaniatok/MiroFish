# Verifikasi E2E Rantai Baru (Zep sebagai Sumber Grounding) — Tahap 1→3→4→5→6

Tanggal: 2026-09-24. Dijalankan dengan `backend/scripts/verify_e2e_zep_pipeline.py` (baru,
dipertahankan seperti `verify_e2e_pipeline.py`). **Tanpa mock**: Zep Cloud sungguhan, Finnhub
sungguhan, LLM sungguhan (`Qwen/Qwen2.5-7B-Instruct-AWQ` RunPod vLLM), yfinance sungguhan,
OASIS sungguhan (2 platform, 10 round, follow-network lengkap). Tidak ada kode diubah untuk
verifikasi ini (1 skrip baru saja); tidak ada commit dibuat.

**Ringkasan super-singkat sebelum detail:** rantai teknis TERSAMBUNG PENUH ujung-ke-ujung
tanpa exception fatal (`run_state.json` → `completed`, `error: null`). **TAPI ditemukan 1 BUG
FUNGSIONAL KRITIS** yang membuat seluruh hasil ekstraksi Zep (114 entity, 12.935 token,
~20 menit kerja Tahap 3) **tidak pernah benar-benar sampai ke LLM** — dikonfirmasi lewat
pembacaan kode DAN eksekusi langsung, bukan dugaan. Ini bukan gap desain yang sudah diketahui
(beda dari temuan `initial_posts`/`sample_articles` yang memang sudah diprediksi sebelum run) —
ini bug implementasi yang baru ditemukan di verifikasi ini.

---

## 0. TEMUAN PALING PENTING — bug di `_build_persona_prompt`, ditemukan saat verifikasi ini

**`_build_persona_prompt(news_lines, grounding)` (`persona_generator.py:350-352`) TIDAK PERNAH
diperbarui untuk mengenali nilai `grounding="zep_graph"`:**

```python
def _build_persona_prompt(news_lines: List[str], grounding: str) -> Tuple[str, str]:
    """Return (system_prompt, user_prompt). grounding=="none" -> tanpa blok berita."""
    if grounding == "news" and news_lines:          # <-- HANYA "news", BUKAN "zep_graph"
        news_block = (...)
        user = _USER_HEAD + _NEWS_INSTRUCTION + news_block + _USER_SCHEMA
    else:
        user = _USER_HEAD + _NO_NEWS_INSTRUCTION + _USER_SCHEMA   # <-- jalur zep_graph JATUH KE SINI
    return _SYSTEM_PROMPT, user
```

**Dikonfirmasi dengan eksekusi langsung** (memanggil fungsi produksi asli, bukan simulasi):
`pg._build_persona_prompt(<114 baris entity Zep asli>, "zep_graph")` menghasilkan prompt yang
**TIDAK mengandung SATU PUN baris entity**, TIDAK ada blok "MARKET NEWS SAMPLE", dan memakai
`_NO_NEWS_INSTRUCTION` ("Draw only on your general understanding... Do not refer to any
specific news event or company development") — **PERSIS jalur "tidak ada grounding sama
sekali"**, walau `grounding.news_lines` yang diteruskan berisi 114 baris nyata.

**Bukti independen dari data run sungguhan yang cocok persis:** RunPod API benar-benar
melaporkan `prompt_tokens=499` untuk panggilan persona generation — sementara 114 entity yang
dibangun `build_grounding_from_graph` seharusnya menyumbang **12.935 token** (diukur presisi
di Tahap 4, tokenizer asli). Reproduksi manual `_build_persona_prompt` dengan `grounding=
"zep_graph"` menghasilkan **486 token** (system+user, tanpa entity) — cocok dalam margin
wajar dengan 499 token yang benar-benar dipakai LLM. **Kesimpulan: 12.935 token hasil kerja
Tahap 3+4 SECARA HARFIAH tidak pernah dikirim ke LLM.**

**Akibat langsung yang menjelaskan SEMUA temuan kualitas di §2 dan sebagian §3 di bawah:**
LLM men-generate 8 persona TANPA melihat entity Zep sama sekali — bukan karena ia "mengabaikan
loose inspiration" seperti instruksi jalur news yang sengaja, tapi karena **inputnya memang
kosong**. Ini mengubah total interpretasi hasil §2: bukan "LLM tidak terpengaruh grounding",
tapi **"grounding tidak pernah tiba"**.

**Ini BUKAN diperbaiki di verifikasi ini** (sesuai instruksi scope) — dilaporkan sebagai
temuan kritis yang harus ditutup SEBELUM rantai ini dipakai untuk kesimpulan apapun soal
"apakah grounding Zep meningkatkan kualitas persona".

---

## Parameter run (untuk reproduksi)

```
sectors          = ["Energy", "Communication Services"]  (sama seperti semua investigasi Zep sebelumnya)
as_of_date       = None (live, 2026-09-24)
simulation_id    = sim_e2e_zep_20260924_141424
persona run_id   = 210cdf6133ed461fbef6e14d6ea2ecf5
graph_id (Zep, dihapus otomatis oleh Tahap 3) = mirofish_universe_live_7b4af093
```

---

## 1. Waktu tempuh tiap tahap (a-f), diukur terpisah

| Tahap | Deskripsi | Waktu | Catatan |
|---|---|---|---|
| **a** | `screen_universe(sectors)` | 36,7 detik | 44 ticker lolos — identik persis dengan baseline investigasi sebelumnya |
| **b** | `build_universe_graph()` (Tahap 3, Zep) | **1.111,5 detik (≈18,5 menit)** | Di dalamnya: `screen_universe` diulang (~37s lagi — Tahap 3 tidak menerima `universe` siap pakai, lihat desain), fetch Finnhub (mayoritas waktu, **14/44 ticker gagal fetch = 32%**, lebih tinggi dari biasanya), ingest Zep murni **393,9 detik** |
| **c** | `build_grounding_from_graph()` (Tahap 4) | 7,3 detik | 114/114 entity **SEMUA muat** di bawah anggaran 15.000 token (terpakai 12.935) — tidak ada yang terpotong run ini |
| **d** | `generate_personas()` (LLM sungguhan) | 152,7 detik | **LEBIH CEPAT dari baseline (291,6s)** meski (seharusnya) prompt jauh lebih besar — konsisten dengan temuan §0: prompt yang BENAR-BENAR dikirim cuma ~499 token, bukan ~13.400 token yang diharapkan |
| **e** | `build_oasis_artifacts()` | 8,0 detik | Identik baseline (8,4s) |
| **f** | `SimulationRunner.start_simulation()` sampai `completed` | **820,0 detik (≈13,7 menit)** | **LEBIH LAMBAT dari baseline (469,2s)** — lihat §4 untuk 8 traceback transien RunPod di akhir run, kemungkinan penyebab |

**Total a-f: 2.136,2 detik ≈ 35,6 menit.**

---

## 2. Kualitas 8 persona — LANGSUNG, tidak dipoles (dibaca dengan konteks bug §0)

**Pola nama — MASIH formulaic, tapi template BERBEDA dari baseline:**

| # | Nama | `philosophy_label` |
|---|---|---|
| 0 | Maggie Market Maven | Volatility Play |
| 1 | Dr. Data Digger | Fundamental Analysis |
| 2 | Mr. Macro Mover | Macro Trends |
| 3 | Ms. Momentum Master | Momentum Trading |
| 4 | Sir Skeptical Steve | Skepticism |
| 5 | Ms. Sentiment Siren | Sentiment Analysis |
| 6 | Mr. Contrarian Captain | Contrarian Investing |
| 7 | Prof. Prolific Polly | Collaborative Research |

Baseline (jalur news) memakai template `FirstName 'The X' LastName` (mis. "Morgan 'The
Momentum' McCloud") untuk **seluruh 8 nama**. Run ini memakai template BERBEDA tapi SAMA
SERAGAMNYA: **Title/Prefix + Nama-Alliteratif** (`Maggie Market Maven`, `Dr. Data Digger`,
`Ms. Momentum Master`, dst — SEMUA 8 nama mengikuti pola "kata kedua & ketiga berima/aliterasi"
yang identik). **Kesimpulan jujur: pivot ke Zep TIDAK mengurangi kecenderungan model jatuh ke
template penamaan seragam — ia cuma berpindah ke template lain yang sama kakunya.** (Catatan
penting: karena bug §0, ini adalah perilaku LLM TANPA grounding sama sekali — tidak bisa
disimpulkan apakah grounding Zep, SEANDAINYA benar-benar sampai, akan mengubah pola ini.)

**Referensi ke fakta konkret dari graph Zep — DICEK LANGSUNG, hasilnya NOL:** mencocokkan
seluruh 114 nama entity (`Ari Emanuel`, `Verizon`... sebenarnya bukan, universe run ini
Energy+Communication Services jadi entity-nya beda: `Netflix`, `Paramount`, `SLB`, `DVN`,
`GOOGL`, `TRGP`, dst) terhadap teks `investment_philosophy`/`personality`/`communication_style`/
`edge_vs_others`/`short_bio` kedelapan persona: **0 (nol) kecocokan entity apapun.** Bahkan
level TEMA sektor (kata "energy"/"oil"/"gas"/"streaming"/"telecom"/"spectrum"/"merger"/
"refin"/"pipeline") juga **0 kemunculan** di seluruh 8 persona — filosofi mereka generik
total ("I dive deep into financial reports", "Global economic trends drive everything").
**Ini konsisten 100% dengan temuan bug §0**: LLM memang tidak pernah melihat entity apapun.

**Keberagaman demografis — LEBIH BAIK dari baseline pada 2 dari 3 sumbu:**

| Metrik | Baseline (news) | Run ini (zep_graph, TAPI grounding tidak sampai) |
|---|---|---|
| MBTI distinct | 6/8 | **7/8** |
| Negara distinct | 5/8 | **6/8** |
| Gender distinct | 3/3 (female/male/other) | 2/2 (female/male saja — TIDAK ada "other") |
| Rentang usia | 28-45 | 29-58 |

**Catatan kejujuran penting:** keberagaman demografis (age/gender/mbti/country) dihasilkan
oleh LANGKAH ENRICHMENT TERPISAH (`build_oasis_artifacts`'s enrichment call, prompt sendiri
yang TIDAK melalui `_build_persona_prompt`/bug §0) — jadi perbedaan MBTI/negara di atas **BUKAN
bukti pengaruh grounding Zep**, murni variasi run-to-run LLM yang non-deterministik (sama
seperti filosofi desain Fase 10b: "non-determinisme yang disengaja").

**Warning Fase 10:** `["event: no_news_seed"]` — **ADA**, tapi ini warning dari `build_event_config`
(Tahap 5, soal `initial_posts` kosong — lihat §3), BUKAN warning duplikat label/nama-archetype
dari `generate_personas` sendiri (field `warnings` di hasil `generate_personas` = `[]`, kosong,
sama seperti baseline).

---

## 3. Cakupan ticker di debat OASIS — JAWABAN TEGAS: BUKAN "tetap sempit", TAPI **NOL TOTAL**

**Dicek eksplisit dari kode SEBELUM run (bukan diasumsikan), dikonfirmasi lagi oleh data run
sungguhan:**

`build_event_config(personas, agent_configs, sample_articles)` (`persona_oasis_adapter.py:586-637`)
memakai **`sample_articles` LAMA** (bentuk artikel Finnhub: `headline`/`ticker`/`article_id`/
`published_at`), **BUKAN** `source_entity_names` yang baru dari Tahap 4. Dipanggil dengan
`personas_result.get("sample_articles") or []` (baris 729) — dan untuk jalur `zep_graph`,
`generate_personas` SELALU mengisi `sample_articles: []` (keputusan desain Tahap 4 §5: field
tidak relevan untuk sumber aktif diisi nilai netral kosong). **Tidak ada jalur apapun yang
menyalurkan `source_entity_names`/entity Zep ke `initial_posts` hari ini** — dikonfirmasi
lewat pembacaan kode, bukan diasumsikan, PERSIS sesuai dugaan awal brief.

**Konsekuensi nyata di run ini:** `chosen` di `build_event_config` kosong → fallback
`WARNING_NO_NEWS_SEED` terpicu → **`initial_posts` = 1 post statis generik**:

> *"Market check-in: what are you watching in US equities right now, and what's your read?"*
> — diposting 1 agent (Ms. Momentum Master), **0 ticker disebut**.

Bandingkan baseline: 4 post dari 4 agent berbeda, MASING-MASING menyebut 1 ticker eksplisit
(`$GOOGL`, `$META`, `$NFLX`, `$CVX`).

**Cakupan ticker SEPANJANG SIMULASI (round manapun, kedua platform), dihitung dari
`actions.jsonl` sungguhan terhadap 44 ticker universe:**

| Platform | Ticker unik dari universe 44 yang disebut |
|---|---|
| Twitter | **0** |
| Reddit | **0** |

**NOL, di KEDUA platform, di SEMUA round.** Bukan cuma "tetap 4-6 seperti sebelumnya" —
**turun ke nol**, LEBIH BURUK dari baseline. Contoh konten nyata (round 1, kedua platform):
*"I'm closely monitoring tech giants' earnings reports"*, *"US equities are looking
overvalued... tech giants and their valuation multiples"*, *"looking at the tech sector and
some promising biotech companies"* — **bahkan level SEKTOR pun salah**: seluruh debat
membahas "tech"/"healthcare"/"biotech"/"semiconductor" secara generik, **TIDAK ADA SATU PUN
menyinggung Energy atau Communication Services** — sektor yang justru jadi dasar universe
44-ticker yang di-screen di awal. Debat ini sepenuhnya lepas dari seluruh pipeline
grounding-nya sendiri.

**Jawaban eksplisit untuk pertanyaan inti brief:** cakupan ticker **TIDAK membaik sama
sekali** — dan alasannya BUKAN SATU, tapi **DUA gap independen yang bertumpuk**:
1. **Bug `_build_persona_prompt`** (§0): grounding Zep tidak pernah sampai ke LLM persona,
   jadi persona sendiri tidak "tahu" apa-apa soal 44 ticker itu.
2. **Gap desain `build_event_config`** (dikonfirmasi kode, independen dari bug #1): bahkan
   SEANDAINYA bug #1 diperbaiki dan persona benar-benar ter-ground di entity Zep,
   `initial_posts` MASIH TIDAK AKAN menyalurkan `source_entity_names` ke seed debat, karena
   `build_event_config` secara struktural hanya membaca `sample_articles` — perlu kerja
   TAMBAHAN eksplisit di Tahap 5 untuk memetakan entity Zep -> initial_posts, ini BUKAN
   otomatis ikut ter-fix hanya dengan memperbaiki bug #1.

**Ini mengoreksi kerangka pertanyaan brief sendiri secara eksplisit** (sesuai brief:
"laporkan ini eksplisit, jangan disamarkan"): tujuan awal pivot ke Zep (representasi 44
ticker) **belum tercapai di TAHAP MANAPUN yang teramati di run ini** — bukan "tercapai di
generate-persona tapi belum di debat", karena bug #1 berarti bahkan tahap generate-persona
pun belum benar-benar menerima grounding-nya.

---

## 4. Regresi teknis

| Item | Status |
|---|---|
| `run_state.json`: `completed`, `total_rounds=10`, `error=null` | ✅ **OK** (`twitter_actions_count=135`, `reddit_actions_count=129`, total 264) |
| Follow-network: 56/56 edge, 7/7 follower+following semua agent, kedua platform | ✅ **OK**, identik baseline |
| Bug `last_rowid` (FOLLOW bocor ke round≥1) | ✅ **TIDAK muncul** — `follow_actions_outside_round0: {}` kedua platform |
| Exception/traceback di `simulation.log` | ⚠️ **8 traceback ditemukan** (baseline: 0) — lihat detail di bawah |
| Cache key: `generate_personas` 2× parameter sama | ✅ **Cache-hit terbukti**: `second_call_from_cache=True`, `run_id` sama, **0 panggilan LLM baru** (`llm_calls_before=1, llm_calls_after=1`) |
| Cache key: beda dari jalur "news" untuk sektor sama | ✅ **Terbukti berbeda**: `universe_key_news=ed0c163a3f246b6f` vs `universe_key_zep_graph=0497433af2c7f004` |

**Detail 8 traceback (BUKAN regresi kode MiroFish, tapi dicatat jujur):** seluruhnya
`openai.InternalServerError: 500 - internal server error` dari endpoint RunPod, terjadi
berturut-turut dalam <1 detik (14:49:50,04x–14:49:51,29x) tepat menjelang `completed_at`
(14:50:01) — kemungkinan lonjakan panggilan LLM paralel dari banyak agent OASIS di round
akhir yang membebani endpoint sesaat. `run_state.json` tetap `completed`/`error: null`,
mengindikasikan `camel`/OASIS punya mekanisme toleransi (retry/skip) untuk kegagalan
keputusan agent individual yang tidak menjatuhkan simulasi secara keseluruhan. **Dicatat
sebagai anomali reliabilitas RunPod sesaat, bukan bug MiroFish** — tapi berbeda dari baseline
yang 100% bersih, jadi tidak disembunyikan di sini.

---

## 5. Estimasi waktu/biaya — rantai baru vs rantai lama

| | Rantai lama (news) | Rantai baru (Zep) |
|---|---|---|
| Tahap persona (a+b+c+d gabungan, atau cuma langkah "generate persona" utk lama) | 291,6s | a+b+c+d = 36,7+1.111,5+7,3+152,7 = **1.308,2s** |
| Artefak OASIS | 8,4s | 8,0s (e) |
| Simulasi OASIS | 469,2s | 820,0s (f) |
| **Total** | **769,2s (≈12,8 menit)** | **2.136,2s (≈35,6 menit)** |

**Rantai baru ≈2,78× lebih lambat dari rantai lama secara total.** Kalau HANYA membandingkan
bagian yang berpadanan langsung (Tahap 3 dikeluarkan, karena tidak ada padanannya di rantai
lama sama sekali): c+d+e+f = 7,3+152,7+8,0+820,0 = 988,0s vs baseline 769,2s → **masih ≈1,28×
lebih lambat** bahkan TANPA menghitung biaya Tahap 3 sama sekali — didorong terutama oleh
simulasi OASIS yang lebih lambat (820,0s vs 469,2s, lihat §4 soal traceback RunPod).

**Kalau Tahap 3 dihitung penuh** (yang memang harus, karena itu satu-satunya alasan rantai
ini ada): tambahan **≈18,5 menit per run** dibanding rantai lama, murni untuk proses Zep yang
—berdasarkan §0—**hasilnya sendiri tidak pernah dipakai LLM di run ini**. Ini adalah biaya
riil (waktu Zep + Finnhub) yang SAAT INI tidak menghasilkan manfaat apapun karena bug §0,
bukan cuma "mahal tapi worth it".

---

## 6. Kesimpulan akhir — TIDAK SIAP sebagai fondasi Tahap 7, ada gap kritis yang harus ditutup dulu

**Rantai teknis TERSAMBUNG PENUH** (screen_universe → build_universe_graph → build_grounding_
from_graph → generate_personas → build_oasis_artifacts → SimulationRunner, semua berhasil,
`error: null`, tidak ada exception fatal, cache key bekerja benar) — bagian "pemasangan pipa"
sudah benar.

**TAPI rantai ini TIDAK SIAP dipakai sebagai fondasi Tahap 7 (ekstraksi sinyal) sampai 2 gap
berikut ditutup:**

1. **[KRITIS, WAJIB DIPERBAIKI DULU] Bug `_build_persona_prompt`** (§0): kondisi
   `if grounding == "news"` harus diperluas mengenali `"zep_graph"` juga (dan idealnya label
   blok "MARKET NEWS SAMPLE" disesuaikan supaya tidak menyesatkan untuk sumber non-berita).
   Tanpa ini, **SELURUH tujuan Tahap 3/4 (memberi LLM bahan grounding yang lebih kaya/luas
   dari 44 ticker) tidak tercapai sama sekali** — LLM persona hari ini efektif berjalan di
   mode "no grounding", terlepas dari berapa banyak/kaya entity yang berhasil diekstrak Zep.
2. **[GAP DESAIN, TERPISAH DARI #1] `build_event_config` tidak menyalurkan entity Zep ke
   `initial_posts`** (§3): perlu kerja desain+implementasi TAMBAHAN di Tahap 5 (bukan
   otomatis ikut selesai begitu #1 diperbaiki) supaya cakupan ticker di seed debat OASIS
   benar-benar merepresentasikan lebih dari segelintir ticker.

**Rekomendasi eksplisit: JANGAN lanjut ke Tahap 7 dari hasil run manapun sebelum #1
diperbaiki DAN diverifikasi ulang** (idealnya lewat skrip `verify_e2e_zep_pipeline.py` yang
sama, run kedua) — mengekstrak "sinyal" dari persona/debat yang groundingnya belum benar-benar
sampai ke LLM akan mengekstrak sinyal dari **noise generik**, bukan dari cakupan 44-ticker
yang jadi seluruh alasan pivot ke Zep.

Tidak ada implementasi/perbaikan dimulai di verifikasi ini (termasuk perbaikan bug §0),
sesuai instruksi scope. Menunggu keputusan.

---

## Lampiran — bukti mentah

Direktori: `backend/uploads/simulations/sim_e2e_zep_20260924_141424/` (semua artefak, DB,
log, dan `e2e_zep_verification_findings.json` lengkap). Skrip verifikasi baru:
`backend/scripts/verify_e2e_zep_pipeline.py` (dipertahankan, pola sama seperti
`verify_e2e_pipeline.py` jalur lama).

```bash
cd backend
python scripts/verify_e2e_zep_pipeline.py \
  --sectors "Energy,Communication Services" \
  --max-wait-seconds 3600
```
