# Fase 11a — Desain Adapter InvestorPersona → Skema OASIS

Status: **desain, belum implementasi** (menunggu review). Tidak ada file kode yang dibuat/diubah
dalam menyusun dokumen ini. Semua nama file/fungsi baru di bawah bersifat **ilustratif**.

Dasar: `docs/investigations/fase11a0_oasis_schema.md` (selanjutnya "investigasi") dan
`docs/design/fase10a_persona_generation.md`. Keputusan yang sudah diambil dan **diikuti apa adanya**:
field dead-config tetap digenerate; `active_hours = list(range(24))` untuk semua 8 persona;
demografis Reddit di-derive per persona lewat **1** panggilan LLM tambahan untuk ke-8 persona sekaligus
(atau heuristik — dipilih salah satu di §3); 10 round; dual-platform; follow-network complete graph.

---

## 0. Temuan baru saat mendesain (memengaruhi keputusan — baca dulu)

Membaca ulang kode untuk menulis dokumen ini menemukan hal-hal yang **tidak tercakup instruksi**
dan harus diputuskan/diketahui reviewer. Ini bukan mengubah keputusan yang sudah diambil, melainkan
syarat agar keputusan itu benar-benar berlaku:

| # | Temuan | Dampak | Ditangani di |
|---|---|---|---|
| A | **Metadata run persona TIDAK menyimpan headline.** `generate_personas` hanya menyimpan `article_ids` (list int); ticker & headline dibuang (`persona_generator.py`, `_save_run` → `article_ids_json`). Instruksi poin 8 mengasumsikan headline bisa diambil dari metadata tanpa fetch ulang. | Poin 8 tidak bisa dipenuhi sebagaimana ditulis. Perlu perubahan **aditif kecil di Fase 10** (simpan `sample_articles`) — atau re-resolve via `get_news_data` (cache). | §8 |
| B | **`active_hours=24` saja TIDAK cukup untuk partisipasi penuh.** `get_active_agents_for_round` mengalikan `target_count` dengan `off_peak_activity_multiplier` (default `0.05`) pada jam `off_peak_hours` (default `[0..5]`), lalu `int(...)`. Dengan 8 agent: `int(8*0.05)=0` → **nol agent aktif** di jam 0–5. Simulasi selalu mulai di jam 0. | Tanpa netralisasi `time_config`, beberapa round pertama kosong walau `active_hours` 24 jam. | §4, §5 |
| C | **`initial_posts` dieksekusi lewat `dict[agent] = ManualAction`** (`run_parallel_simulation.py:1180-1200`): dua post dari agent yang sama saling menimpa (hanya yang terakhir dieksekusi, tapi keduanya dicatat di log). Standalone `run_reddit_simulation.py` menangani ini (list per agent), `run_twitter_simulation.py` dan parallel tidak. | "Semua post ke persona influence tertinggi" akan membuang post. **Setiap initial post harus dari agent berbeda.** | §8 |
| D | `action_logger.py:98` dan `:271` menulis `total_rounds = total_simulation_hours * 2` (hard-code 30 menit/round). Itulah asal `144` (=72×2) di log lama. | Dengan 10 jam × 60 menit, log akan berkata 20 round padahal 10. **Pilih 5 jam × 30 menit**, bukan 10 × 60 — semua formula setuju. | §5 |
| E | `POST /api/simulation/start` memanggil `manager.get_simulation()` dan `_check_simulation_prepared()` yang butuh **`state.json`** (`status` ∈ ready/…, `config_generated=true`) di samping 3 artefak. `SimulationRunner.start_simulation` sendiri hanya butuh `simulation_config.json`. | Artefak ke-4 (`state.json`) wajib bila memakai jalur API. | §7 |
| F | `fetch_new_actions_from_db` dimulai `last_rowid=0` dan **tidak dimajukan setelah round 0**. Aksi round 0 (initial posts; kelak 56 FOLLOW) akan terbaca lagi dan tercatat sebagai aksi round 1. | Log `actions.jsonl` round 1 tercemar; hitungan aksi salah. Harus dimajukan saat implementasi. | §6 |
| G | Di OASIS 0.2.5, `perform_agent_graph_action` **di-comment** di `agent.py:150`, dan jalur `ManualAction` tidak menyentuh `AgentGraph` sama sekali. Yang menentukan feed adalah **tabel `follow` di SQLite**; edge `igraph` kosmetik. Tabel `follow` hanya dibaca jalur refresh **Twitter** (`platform.py:288`); recsys Reddit melewatinya. | Follow-network berpengaruh nyata terutama di Twitter. Tetap dibuat di kedua platform (keputusan sudah diambil), tapi ekspektasi dampak Reddit rendah. | §6 |

---

## 1. Komposisi teks persona (`user_char` Twitter / `persona` Reddit)

### Aturan (deterministik, fungsi murni dari `InvestorPersona`)

Satu fungsi `compose_persona_text(p) -> str` menghasilkan **satu string tanpa newline**, dipakai **identik**
untuk `user_char` (CSV) dan `persona` (JSON). Bagian digabung dengan satu spasi, **urutan tetap**:

| # | Bagian | Template | Bila kosong |
|---|---|---|---|
| 1 | Pembuka (konstan) | `{name} is an independent investor who shares views on US stocks and market news on social media.` | — |
| 2 | Tagline | `Tagline: {tagline}` | — (wajib) |
| 3 | Filosofi | `Investment philosophy ({philosophy_label}): {investment_philosophy}` | — (wajib) |
| 4 | Watak | `Personality: {personality}` | — (wajib) |
| 5 | Gaya tulis | `Communication style (how {name} writes posts): {communication_style}` | — (wajib) |
| 6 | Bias | `Known biases: {b1}; {b2}; …` (dipisah `"; "`, urutan list asli) | — (wajib, 2–4 item) |
| 7 | Pembeda | `What sets {name} apart from the other investors: {edge_vs_others}` | — (wajib) |
| 8 | Latar | `Background: {short_bio}` | **bagian dihilangkan** bila `short_bio` None |

Normalisasi tiap bagian (juga deterministik): `" ".join(s.split())` (menghilangkan newline/tab/spasi ganda —
wajib karena `csv` writer MiroFish mengganti newline dengan spasi), lalu bila karakter terakhir bukan
`. ! ? …` tambahkan `"."`. Emoji dan karakter non-ASCII **dipertahankan** (file ditulis UTF-8).

**Alasan urutan:** identitas → cara berpikir (filosofi, watak) → cara bertindak/menulis (gaya, bias) →
pembeda → latar. Gaya tulis dan bias diletakkan setelah watak karena itulah yang paling menentukan
*isi post* yang dihasilkan agent; latar (opsional, paling tidak relevan bagi perilaku) di akhir.
Persona bercerita orang ketiga dengan nama eksplisit — konsisten dengan contoh nyata run lama
(`"Kurtz is a cybersecurity expert…"`), dan system prompt OASIS Twitter menambahkan `Your name is {username}.`
sendiri.

**Yang sengaja TIDAK dimasukkan:** stance/skor/verdict apa pun (Fase 11b), dan instruksi perilaku
("selalu posting BUY") — teks hanya deskriptif.

**Reproducibility:** `compose_persona_text(p)` tidak punya input lain (tanpa waktu/random), jadi
`InvestorPersona` yang sama → string yang sama byte-per-byte.

### Contoh konkret (persona ilustratif Skenario A 10a: "Dex Okonkwo-Hale"; isi field **karangan ilustratif**, bukan output LLM)

Input:
```
name                 = "Dex Okonkwo-Hale"
tagline              = "Rides whatever is going up and bails the second it cracks."
philosophy_label     = "momentum chaser"
investment_philosophy= "Price is the only truth that pays. Dex buys strength, adds on breakouts and cuts losers within days; fundamentals are a story people tell after the move. Horizon is days to a few weeks."
biases               = ["recency bias", "FOMO on breakouts", "overconfidence after a winning streak"]
personality          = "Restless, competitive and quick to celebrate. Treats every trade like a scoreboard and gets visibly rattled when a position stalls."
communication_style  = "Short, loud posts with ALL-CAPS bursts and emojis like 🚀🔥. Announces entries and exits in real time and rarely explains beyond the chart."
edge_vs_others       = "The only persona who acts purely on price momentum and is comfortable reversing within a day."
short_bio            = "Ex-options desk runner turned full-time retail trader based in London."
```
Output (1 baris, dipotong di sini hanya untuk tampilan):
```
Dex Okonkwo-Hale is an independent investor who shares views on US stocks and market news on social media. Tagline: Rides whatever is going up and bails the second it cracks. Investment philosophy (momentum chaser): Price is the only truth that pays. Dex buys strength, adds on breakouts and cuts losers within days; fundamentals are a story people tell after the move. Horizon is days to a few weeks. Personality: Restless, competitive and quick to celebrate. Treats every trade like a scoreboard and gets visibly rattled when a position stalls. Communication style (how Dex Okonkwo-Hale writes posts): Short, loud posts with ALL-CAPS bursts and emojis like 🚀🔥. Announces entries and exits in real time and rarely explains beyond the chart. Known biases: recency bias; FOMO on breakouts; overconfidence after a winning streak. What sets Dex Okonkwo-Hale apart from the other investors: The only persona who acts purely on price momentum and is comfortable reversing within a day. Background: Ex-options desk runner turned full-time retail trader based in London.
```

---

## 2. `username`, `agent_id`, `bio`/`description`

### `agent_id`
Index urut `0..N-1` sesuai **urutan list `personas` apa adanya** dari `get_or_generate_personas` (N = 8,
`PERSONA_COUNT`). **Index yang sama HARUS dipakai di ketiga artefak** karena OASIS tidak memverifikasi silang
(investigasi §2.D): `agent_id = posisi baris CSV = posisi elemen JSON = agent_configs[i].agent_id =
initial_posts[].poster_agent_id = followee_id`. Kolom/field `user_id` tetap ditulis (= index) demi
kompatibilitas pembaca lama meski OASIS mengabaikannya. Adapter **wajib meng-assert** `len == 8` dan bahwa
ketiga artefak dibangun dari **satu list terurut yang sama** (satu variabel `personas`, di-enumerate sekali).

### `username` — algoritma slug (dijalankan berurutan atas `name`, per index 0..N-1)

1. `unicodedata.normalize("NFKD", name)`, buang combining mark (aksen → ASCII: `Ilse`, `Zoë`→`Zoe`).
2. `casefold()` → lowercase.
3. Ambil token `re.findall(r"[a-z0-9]+", s)`; gabungkan dengan `"_"` (spasi, titik, tanda hubung, apostrof, emoji
   → hilang/menjadi pemisah).
4. Potong ke maks **30 karakter**, lalu `rstrip("_")`.
5. Bila hasil kosong (mis. nama seluruhnya non-Latin) → `"investor"`.
6. **Tabrakan** (slug sudah dipakai persona index lebih kecil dalam run yang sama): tambahkan `f"_{index}"`
   (index persona yang bertabrakan). Bila masih bertabrakan (patologis), tambahkan `_2`, `_3`, … sampai unik.
   Persona pertama dengan slug itu **tidak** diberi suffix.

Contoh: `"Marisol Vance"`→`marisol_vance`, `"Dex Okonkwo-Hale"`→`dex_okonkwo_hale`,
`"Dr. Ilse Brandvold"`→`dr_ilse_brandvold`, `"Tante Rina"`→`tante_rina`.
Tabrakan: `"Marisol-Vance"` (index 5) setelah `"Marisol Vance"` (index 0) → `marisol_vance_5`.
Nama sudah dijamin unik (casefold) oleh Fase 10, jadi tabrakan hanya mungkin dari penyederhanaan slug.
Catatan: OASIS menampilkan `username` sebagai `UserInfo.name` (nama tampil di system prompt & DB; lihat
investigasi §2.D soal `user_name` NULL) — bukan `name` asli.

### `bio` / `description`
`short_bio` bila ada isinya; jika `None` → `tagline` (sesuai investigasi §8). Batas **150 karakter**
(mengikuti `bio[:150]` writer lama): bila lebih, potong di spasi terakhir sebelum karakter ke-147 lalu
tambah `"…"`, bukan memotong di tengah kata. Aturan sama untuk `description` (Twitter) dan `bio` (Reddit).
Tidak ada informasi hilang karena teks penuh ada di `user_char`/`persona`.

---

## 3. Demografis Reddit — DIPILIH: **1 panggilan LLM batch**

### Keputusan dan alasan
**LLM batch**, bukan heuristik. Alasan:
1. **Sinyal leksikal tidak cukup.** `age`, `gender`, `country` hampir tak punya sinyal di teks
   personality/philosophy; keyword→MBTI hanya menebak dari kata kunci, dan hasilnya cenderung konvergen
   (mis. semua "cautious" → ISTJ). Heuristik berisiko **dangkal dan seragam**.
2. **Alasan yang sama dengan Fase 10:** satu panggilan yang melihat ke-8 persona sekaligus bisa menyebar
   usia/negara/MBTI secara sadar; 8 panggilan terpisah cenderung konvergen.
3. **Biaya kecil:** input ≈ 8 × ~150 token, output ≈ 8 × ~40 token. Satu titik kegagalan LLM tambahan diterima
   dan dimitigasi retry + cache (di bawah).
4. Panggilan yang sama sekaligus menghasilkan **4 rating ordinal kasar** (1–5) yang dipetakan deterministik
   ke field aktivitas (§4). Ini bukan pendekatan kedua untuk demografis; ini menghindari heuristik keyword
   untuk `influence_weight`, satu-satunya field aktivitas yang sebagian hidup.

Trade-off yang diterima: 1 titik kegagalan tambahan. **Tidak ada fallback statis/heuristik bila 3 attempt
gagal** — konsisten dengan Fase 10 ("tanpa fallback template dan tanpa persona parsial"): adapter mengembalikan
`success=False` dan **tidak menulis artefak apa pun**. (Mengisi diam-diam dengan default seperti `country="中国"`
milik writer lama adalah persis pola yang harus dihindari.)

### Persistence (agar reproducible)
Hasil disimpan **per `run_id` persona** (tabel baru `persona_oasis_enrichment` di `persona_runs.db`
yang sama, append-only: `run_id, generated_at, model, temperature_used, attempt, profiles_json, warnings_json`).
Adapter memakai yang tersimpan bila ada. Tanpa ini, dua simulasi dari **persona run yang sama** (yang memang
sengaja dipakai ulang untuk perbandingan adil, 10a §4) akan mendapat demografis berbeda.
Parameter `force_regenerate_enrichment=False` mengizinkan regenerasi eksplisit.

### Prompt (1 panggilan, dalam bahasa Inggris seperti prompt Fase 10)

**System:**
```
You are a casting director for a social-media investing simulation. You are given
8 FICTIONAL investor personas. For each one, assign plausible demographic details and a few coarse
behavioral ratings, inferred from the persona's own text. These are invented characters, not real
people. Do not rewrite or restate the personas.
```

**User:**
```
Below are 8 fictional investor personas (index 0-7). For EACH persona, return demographics and
behavioral ratings that fit the character.

PERSONAS:
[0] name: {name}
    philosophy_label: {philosophy_label}
    investment_philosophy: {investment_philosophy}
    personality: {personality}
    communication_style: {communication_style}
    edge_vs_others: {edge_vs_others}
[1] ...
[7] ...

RULES:
- Judge each persona by their OWN text; do not give everyone a similar profile. Spread ages, genders,
  MBTI types and countries across the set the way a real community of retail and professional
  investors would be spread.
- age: integer 21-85.
- gender: exactly one of "male", "female", "other".
- mbti: exactly one of the 16 valid types (INTJ, INTP, ENTJ, ENTP, INFJ, INFP, ENFJ, ENFP, ISTJ, ISFJ,
  ESTJ, ESFJ, ISTP, ISFP, ESTP, ESFP), uppercase, consistent with the persona's temperament.
- country: English country name where the persona lives. They all follow the US stock market, so being
  US-based is common, but international investors (expats, foreign retail investors trading US equities)
  are equally plausible. Use a realistic mix across the 8 with at least 3 different countries. Treat a
  persona's name as only a weak hint and avoid stereotyping.
- posting_intensity 1-5: how often they post (1 = rarely, 5 = constantly).
- reactivity 1-5: how fast they react to news (1 = slow and deliberate, 5 = instant).
- optimism 1-5: default emotional tone (1 = habitually pessimistic, 3 = neutral, 5 = habitually upbeat).
- influence_level 1-5: how forcefully they command attention in a social feed, judged from
  communication_style and edge_vs_others (1 = easily ignored, 5 = dominates the conversation).
- Echo back the same index and name you were given for each item.

Return JSON:
{"profiles": [
  {"index": 0, "name": "...", "age": 34, "gender": "female", "mbti": "INTJ",
   "country": "United States", "posting_intensity": 3, "reactivity": 2,
   "optimism": 2, "influence_level": 3},
  ... exactly 8 items ...
]}
```

### Validasi struktural (hard → retry) dan soft-check (warning saja)

Hard, semuanya harus lolos untuk ke-8 item:
1. JSON ter-parse (reuse `_repair_json` Fase 10; `finish_reason=="length"` → coba perbaiki).
2. Key `profiles` ada, list, panjang **persis 8**.
3. Tiap item: `index == posisinya` **dan** `name` sama dengan persona (`strip().casefold()`) — mencegah
   demografis tertukar antar persona.
4. `age`: int (bukan bool; float bulat diterima dan di-cast), `21 ≤ age ≤ 85`.
5. `gender.strip().casefold()` ∈ `{"male","female","other"}` (disimpan lowercase).
6. `mbti.strip().upper()` ∈ 16 tipe valid.
7. `country`: string non-kosong ≤ 60 karakter setelah `strip()`; alias `US/USA/U.S./United States of America` → `United States`.
8. `posting_intensity, reactivity, optimism, influence_level`: int `1..5`.

Soft (dicatat di `warnings`, **tidak** memicu retry — pola `_soft_checks` Fase 10): `<4` MBTI berbeda;
`<3` negara berbeda; semua gender sama; salah satu dari 4 rating bernilai sama di ke-8 persona (indikasi collapse).

### Retry — reuse pola Fase 10 persis
`max_attempts=3`; `temperature = _temperature_for_attempt(attempt)` (`1.0, 0.9, 0.8`, lantai 0.8; **fungsi yang
sama diimpor**, bukan disalin); `create_chat_completion(..., response_format={"type": "json_object"})`
tanpa `max_tokens`; exception LLM → `time.sleep(1*(attempt+1))` lalu attempt berikut; validasi gagal → attempt
berikut dengan pesan error spesifik di log. Gagal 3× → `{"success": False, "error": "..."}`.

---

## 4. `AgentActivityConfig` — semua field

### ⚠️ Peringatan wajib (dari investigasi 11a-0): mayoritas field TIDAK berpengaruh
Berdasarkan investigasi §2.C (grep nol konsumen di `backend/scripts/` dan `backend/app/`), **hanya
`agent_id`, `entity_name`, `activity_level`, `active_hours` yang dibaca skrip runner.**
**`response_delay_min`, `response_delay_max`, `sentiment_bias`, `stance`, `posts_per_hour`,
`comments_per_hour`, `entity_uuid`, `entity_type` dan seluruh `PlatformConfig` (`twitter_config`,
`reddit_config`) adalah DEAD CONFIG: digenerate untuk kelengkapan, TIDAK mengubah simulasi sama sekali**
kecuali runner dimodifikasi untuk membacanya. `influence_weight` hanya "setengah hidup": ia tidak dibaca
runner, tetapi kita memakainya sendiri di adapter (§8) untuk memilih poster `initial_posts`.
Siapa pun yang membaca `simulation_config.json` hasil adapter jangan menganggap field-field ini menggerakkan
perilaku. Adapter sebaiknya menulis catatan ini ke `generation_reasoning` di config.

### Field yang benar-benar dibaca

| Field | Nilai | Alasan |
|---|---|---|
| `agent_id` | index 0..7 (§2) | kunci `env.agent_graph.get_agent()` |
| `entity_name` | `persona.name` | label di `actions.jsonl` (`get_agent_names_from_config`) |
| `active_hours` | `list(range(24))` untuk **semua** agent | **keputusan sudah diambil** |
| `activity_level` | **`1.0`** untuk semua agent | lihat di bawah |

**`activity_level = 1.0` (rekomendasi).** Loop aktivasi: `if random.random() < activity_level: candidates.append(...)`.
`random.random()` ∈ [0,1), jadi `1.0` ⇒ agent **selalu** lolos filter. Alasan:
- `active_hours` hanya membuka *jam*; `activity_level` adalah lemparan koin **per agent per round**
  *setelah* jam terbuka. Keduanya saling melengkapi: 24 jam dengan 0.5 masih membuat rata-rata 4 dari 8 agent
  diam tiap round; dari 10 round itu berarti debat yang bolong-bolong.
- Dengan 0.9, `P(kedelapan aktif) = 0.9⁸ ≈ 0.43` — 57% round kehilangan ≥1 suara; dengan 0.8 hanya ≈17% round
  lengkap. Untuk tujuan "partisipasi penuh" dan 10 round pendek, ambang yang masuk akal hanya `1.0`.
- Menghapus sumber non-determinisme (koin) mengurangi noise antar-run backtest (Fase 11b/c perlu
  perbandingan); LLM tetap sumber variasi utama. Bila kelak ingin heterogenitas, `0.85–0.95` per persona
  boleh — di luar keputusan ini.

### Netralisasi `time_config` (syarat agar keputusan 24 jam benar-benar berlaku — temuan B)
Selain `active_hours`, `get_active_agents_for_round` membaca `time_config`. Agar tidak ada round kosong:
```json
"time_config": {
  "total_simulation_hours": 5, "minutes_per_round": 30,
  "agents_per_hour_min": 8, "agents_per_hour_max": 8,
  "peak_hours": [], "peak_activity_multiplier": 1.0,
  "off_peak_hours": [], "off_peak_activity_multiplier": 1.0,
  "morning_hours": [], "morning_activity_multiplier": 1.0,
  "work_hours": [], "work_activity_multiplier": 1.0
}
```
`peak_hours`/`off_peak_hours` **harus ada sebagai list kosong** (bukan dihapus): skrip memakai
`time_config.get("peak_hours", [9,10,11,…])`, jadi key yang hilang akan mengembalikan default MiroFish dan
mengaktifkan multiplier lagi; list kosong membuat `current_hour in []` selalu False → `multiplier = 1.0`.
`agents_per_hour_min = max = N` (=8) ⇒ `target_count = int(uniform(8,8)*1.0) = 8` ⇒ `random.sample(candidates, min(8, 8))`
memilih semuanya. (Jumlah 8 harus sama dengan `len(personas)`; adapter mengisinya dari `len(personas)`.)
Hasil: **8 agent aktif di setiap round, di kedua platform.**

### Field dead config — diturunkan dari 4 rating ordinal (§3), deterministik

Rating LLM (`posting_intensity` P, `reactivity` R, `optimism` O, `influence_level` I; semua 1–5) dipetakan tabel tetap.
Tidak presisi (tidak dikonsumsi), tetapi **konsisten**: persona yang "hype-driven, loud" (P=5, R=5, I=4) mendapat
posting tinggi, delay pendek, pengaruh besar; persona akademis-lambat (P=2, R=1) sebaliknya.

| Field | Rumus / tabel | Rentang hasil | Status |
|---|---|---|---|
| `posts_per_hour` | `round(0.2 * P, 1)` | 0.2 … 1.0 | dead |
| `comments_per_hour` | `round(0.4 * P, 1)` (rasio 1:2 seperti default dataclass) | 0.4 … 2.0 | dead |
| `response_delay_min` / `_max` (menit simulasi) | R→(min,max): `5→(1,10)`, `4→(3,20)`, `3→(5,45)`, `2→(15,90)`, `1→(60,240)` | — | dead |
| `sentiment_bias` | `round((O - 3) * 0.25, 2)` | −0.5 … +0.5 (sempit; ini *baseline temperamen*, bukan pandangan pasar) | dead |
| `stance` | **`"neutral"` untuk semua** | — | dead |
| `entity_type` | `"Investor"` | — | dead |
| `entity_uuid` | `uuid5(NAMESPACE_URL, f"{run_id}:{agent_id}")` | deterministik | dead |
| `influence_weight` | I→`{1: 0.6, 2: 0.9, 3: 1.2, 4: 1.8, 5: 2.5}` | 0.6 … 2.5 | **sebagian hidup** (§8) |

Kenapa `stance="neutral"` statis dan bukan diturunkan: Fase 10 **sengaja** tidak memberi persona stance;
stance terhadap ticker baru muncul saat berdebat (Fase 11b). Mengisi `supportive/opposing` di sini akan
mengarang stance yang kontradiktif dengan desain tersebut. Field ini dead sehingga nilai netral tidak berdampak.

`influence_weight` diturunkan dari `influence_level` yang dinilai LLM **dari `communication_style` dan
`edge_vs_others`** (intensitas komunikasi + pembeda) — bukan kata kunci — sehingga pemilihan poster awal tidak arbitrer.
Nilai 0.6–2.5 sejalan dengan skala contoh MiroFish (0.8–3.0).

### `PlatformConfig` (level environment; dead)
Diisi nilai default dataclass MiroFish, ditandai dead di `generation_reasoning`:
```json
"twitter_config": {"platform": "twitter", "recency_weight": 0.4, "popularity_weight": 0.3, "relevance_weight": 0.3, "viral_threshold": 10, "echo_chamber_strength": 0.5},
"reddit_config":  {"platform": "reddit",  "recency_weight": 0.4, "popularity_weight": 0.3, "relevance_weight": 0.3, "viral_threshold": 10, "echo_chamber_strength": 0.5}
```
Perilaku recsys sebenarnya **hard-coded** di `OasisEnv.__init__` (investigasi §4); nilai di atas tidak mengubahnya.

---

## 5. Konfigurasi round — PERSIS 10 round

**Pilihan: `total_simulation_hours = 5`, `minutes_per_round = 30`, `max_rounds` tidak diberikan.**

Verifikasi manual — setiap tempat yang menghitung jumlah round:

| Lokasi | Formula | Hitungan |
|---|---|---|
| `simulation_runner.py:412` (`run_state.total_rounds`) | `int(total_hours * 60 / minutes_per_round)` | `int(5*60/30) = int(10.0) = 10` |
| `run_parallel_simulation.py:1195` (loop nyata; sama di `run_reddit…`) | `(total_hours * 60) // minutes_per_round` | `300 // 30 = 10` |
| `action_logger.py:98` / `:271` (`simulation_start.total_rounds`) | `total_simulation_hours * 2` | `5 * 2 = 10` ✔ |
| default skrip bila key hilang | `minutes_per_round` default `30` | konsisten dengan 30 |

Keempatnya = **10**. (Tidak ada masalah pembulatan float: 300/30 habis dibagi.)

**Kenapa bukan 10 jam × 60 menit** (contoh di instruksi): hitungan runner & loop tetap 10, tetapi
`action_logger` (hard-coded `*2`, mengasumsikan 30 menit/round) akan menulis `total_rounds: 20` — ketidakcocokan
log persis seperti kasus lama (`144` vs `10`). 5 jam × 30 menit membuat **semua** angka setuju tanpa menyentuh `action_logger`.

**Kenapa tanpa `max_rounds` sebagai pemotong:** (1) satu sumber kebenaran — jumlah round hanya ditentukan
`time_config`, tidak ada dua angka (config vs argumen CLI) yang bisa berbeda; (2) tidak ada log yang menyebut
angka "config" berbeda dari "aktual" (pola lama: 72 jam dipotong 10 → log `144`, `run_state` `10`);
(3) `start_simulation(max_rounds=None)` tidak perlu diteruskan dari UI/API, jadi tidak ada risiko argumen
terlewat yang diam-diam menjalankan 10 jam × 2 = 10 round dalam config berbeda; (4) config hasil adapter
bisa dipahami sendiri tanpa konteks pemanggil. (`simulated_hour` = `(round*30//60)%24` bernilai 0,0,1,1,…,4,4 —
tidak relevan karena §4 menetralkan semua kepekaan jam.)

Round 0 (follow-network + `initial_posts`) **di luar** hitungan 10: tercatat sebagai `round: 0` di log,
seperti pola existing. Jadi total 10 round LLM + 1 fase seeding.

---

## 6. Follow-network complete graph — desain

### Pendekatan
Di round 0, **sebelum** post awal, inject satu `env.step` berisi `ManualAction(FOLLOW)` untuk setiap
pasangan berarah `(i → j)`, `i ≠ j`: `8 × 7 = 56` directed edge (= 28 pasangan × 2 arah), `agent_id` 0..7 saling follow.
`env.step` menerima `dict[agent → list[ManualAction]]` (investigasi §5; `OasisEnv.step` menangani nilai bertipe list),
jadi satu step cukup — tidak perlu 56 step.

Argumen: `ActionType.FOLLOW` dengan `{"followee_id": j}` (signature `SocialAction.follow(self, followee_id: int)`).
Untuk agent dari graph builder, `user_id == agent_id` (`sign_up` menyisipkan `user_id = agent_id`), jadi
`followee_id` = `agent_id` target.

Karena `perform_agent_graph_action` dimatikan di 0.2.5 (temuan G) dan jalur `ManualAction` tidak menyentuh
`AgentGraph`, adapter juga memanggil `env.agent_graph.add_edge(i, j)` agar struktur in-memory konsisten
dengan tabel `follow` (pola `generate_controllable_agents`). Ini kosmetik; yang menentukan feed adalah tabel `follow`.

### Gating agar jalur lama tidak berubah
Perilaku diaktifkan **hanya bila config meminta**: key top-level baru di `simulation_config.json`
```json
"social_graph": {"follow_network": "complete"}
```
Key hilang / bukan `"complete"` → perilaku lama persis (tidak ada follow di round 0). Skrip lama hanya memakai
`.get()`, jadi key tambahan tidak merusak. Ini menjawab risiko "file yang sama dipakai jalur lama":
simulasi lama (`uploads/simulations/sim_*`) dan jalur Zep tidak memuat key ini → tidak terpengaruh.

### Pseudocode (ilustratif, BUKAN kode yang dijalankan)

Titik sisip di `run_twitter_simulation` (`run_parallel_simulation.py`): **antara `log_round_start(0, 0)`
(baris 1176-1177) dan blok `if initial_posts:` (baris ~1180)**. Untuk `run_reddit_simulation`: antara
baris 1367-1368 dan `if initial_posts:` (~1371). Posisi ini setelah `env.reset()` (1162 / 1353; agent
sudah `sign_up`) dan setelah `simulation_start` dilog, sehingga aksi follow tercatat sebagai `round 0`.

```python
# ── sisip setelah:  action_logger.log_round_start(0, 0)
if config.get("social_graph", {}).get("follow_network") == "complete":
    agent_ids = sorted(a for a, _ in result.env.agent_graph.get_agents())   # 0..7
    follow_actions = {}
    for i in agent_ids:
        me = result.env.agent_graph.get_agent(i)
        follow_actions[me] = [
            ManualAction(action_type=ActionType.FOLLOW, action_args={"followee_id": j})
            for j in agent_ids if j != i                                     # 7 per agent
        ]
    await result.env.step(follow_actions)                                    # 56 aksi, 1 step
    for i in agent_ids:
        for j in agent_ids:
            if i != j:
                result.env.agent_graph.add_edge(i, j)                        # sinkron igraph (kosmetik)

    # majukan pembaca trace supaya FOLLOW tidak bocor ke round 1 (temuan F),
    # dan catat sebagai round 0:
    rows, last_rowid = fetch_new_actions_from_db(db_path, last_rowid, agent_names)
    for r in rows:
        action_logger.log_action(round_num=0, agent_id=r["agent_id"], agent_name=r["agent_name"],
                                 action_type=r["action_type"], action_args=r["action_args"])
        total_actions += 1; initial_action_count += 1
# ── lalu blok `if initial_posts:` yang ada (tidak diubah), lalu log_round_end(0, …), lalu loop round
```
Setelah blok `initial_posts`, pemanggilan `fetch_new_actions_from_db` yang sama sebaiknya diulang untuk memajukan
`last_rowid` juga bagi post awal (mengatasi duplikasi yang sudah ada — lihat catatan F).
Urutan **follow lebih dulu, post kemudian** disengaja: `update_rec_table()` di awal `step` serta query feed
following Twitter membaca tabel `follow`; post awal harus terbit setelah jaringan ada agar follower langsung
melihatnya di feed.

### Catatan risiko (harus diverifikasi sebelum implementasi — tidak didesain di sini)
1. **Modifikasi ini menyentuh `run_parallel_simulation.py` (fungsi `run_twitter_simulation` dan
   `run_reddit_simulation`) dan, bila runner dipanggil per platform, padanannya di `run_twitter_simulation.py`
   (blok initial events ~baris 604-625) dan `run_reddit_simulation.py` (~593-615).** Berkas-berkas ini juga dipakai
   jalur lama mana pun yang memanggil `SimulationRunner.start_simulation` / endpoint `/api/simulation/start`
   dengan `platform=twitter|reddit|parallel` di luar Fase 11. Gating `social_graph` di atas menjaga perilaku lama,
   **tetapi harus dibuktikan** (mis. menjalankan config lama tanpa key itu dan memastikan log identik).
   Bila hanya `parallel` yang dimodifikasi, mode `twitter`/`reddit` tunggal akan berjalan **tanpa** jaringan
   follow (tidak crash) — keputusan apakah ketiganya harus diubah ada di reviewer.
2. Konkurensi: 56 `ManualAction` dijalankan lewat `asyncio.gather`; asumsi bahwa `Platform.running()` memproses
   pesan channel berurutan (sehingga aman terhadap `self.db_cursor` bersama) **belum diverifikasi**.
3. Follow yang duplikat mengembalikan `{"success": False, "error": "Follow record already exists."}` — tidak
   crash, tetapi berarti aksi ini idempoten.
4. Dampak Reddit rendah (temuan G): tabel `follow` hanya dibaca jalur refresh non-Reddit (`platform.py:288`).
   Follow tetap dibuat di Reddit (keputusan sudah diambil), tetapi jangan mengharapkan feed berubah drastis.

---

## 7. Config generation TANPA Zep — jalur baru

### Fungsi baru (ilustratif)
File baru (nanti): `backend/app/services/persona_oasis_adapter.py`.

```python
def build_oasis_artifacts(
    personas_result: dict,                        # keluaran get_or_generate_personas (harus success=True)
    *,
    simulation_id: Optional[str] = None,          # default: f"sim_{uuid4().hex[:12]}" (pola SimulationManager)
    sim_root: Optional[str] = None,               # default: Config.OASIS_SIMULATION_DATA_DIR
    force_regenerate_enrichment: bool = False,
) -> dict:
    """→ {"success": True, "simulation_id", "sim_dir", "files": {...}, "warnings": [...],
          "persona_run_id", "as_of_date"}   |   {"success": False, "error": "..."}"""
```
Fungsi pembantu (murni kecuali yang bertanda LLM/IO):
`compose_persona_text(p)` (§1) · `derive_usernames(personas)` (§2) · `short_description(p)` (§2) ·
`get_or_generate_enrichment(run_id, personas, force)` (§3; **LLM + IO**) · `map_activity(enrichment)` (§4) ·
`build_time_config(n)` (§4/§5) · `build_event_config(personas_result, agent_configs)` (§8) ·
`write_artifacts(sim_dir, …)` (**IO**).

Urutan kerja & sifat all-or-nothing: `personas_result["success"]` false → return error (tidak ada IO).
Enrichment gagal → return error **sebelum** menulis apa pun. Semua isi dibangun **di memori** dulu, baru ditulis;
tiap file ditulis ke `*.tmp` lalu `os.replace` (atomik), dengan **`state.json` ditulis TERAKHIR** karena
ia yang menandai kesiapan.

### Struktur folder & nama file persis
Root = `Config.OASIS_SIMULATION_DATA_DIR` = `backend/uploads/simulations` (sama dengan `SimulationRunner.RUN_STATE_DIR`).
```
backend/uploads/simulations/<sim_id>/
├── twitter_profiles.csv        # dibaca run_*_simulation.py (nama tetap)
├── reddit_profiles.json        # dibaca run_*_simulation.py (nama tetap)
├── simulation_config.json      # prasyarat SimulationRunner.start_simulation (satu-satunya yang dicek runner)
└── state.json                  # prasyarat jalur API /start (manager.get_simulation + _check_simulation_prepared)
```
Direktori runtime (`twitter/`, `reddit/`, `ipc_commands/`, `ipc_responses/`, `*.db`, `simulation.log`,
`run_state.json`, `env_status.json`) dibuat runner/skrip sendiri — **adapter tidak menyentuhnya**.
`cwd` skrip = `sim_dir` (di-set `Popen(cwd=sim_dir)`), sehingga `./log/` dibuat relatif ke sana (§9).

Kecocokan dengan prasyarat: `start_simulation` → `os.path.join(RUN_STATE_DIR, sim_id, "simulation_config.json")`
harus ada; skrip → `twitter_profiles.csv` / `reddit_profiles.json` di `dirname(--config)`;
`_check_simulation_prepared` → 4 file di atas + `status ∈ {ready,…}` **dan** `config_generated=true`.

### Isi `state.json` (bentuk `SimulationState.to_dict`)
```json
{"simulation_id": "<sim_id>", "project_id": "", "graph_id": "",
 "enable_twitter": true, "enable_reddit": true, "status": "ready",
 "entities_count": 8, "profiles_count": 8, "entity_types": ["Investor"],
 "profiles_generated": true, "config_generated": true,
 "config_reasoning": "Generated by persona_oasis_adapter from persona run <run_id>; no Zep.",
 "current_round": 0, "twitter_status": "not_started", "reddit_status": "not_started",
 "created_at": "<iso>", "updated_at": "<iso>", "error": null}
```
`project_id`/`graph_id` dikosongkan karena hanya dibutuhkan `enable_graph_memory_update=True` (default False;
jalur Zep memang dilewati). Field kompatibilitas/yang belum diverifikasi ada di §9.

### Isi `simulation_config.json` (top-level)
```
simulation_id, project_id:"", graph_id:"", simulation_requirement:<teks tetap ringkas>,
time_config      (§4/§5),
agent_configs    (8 entri, §4),
event_config     {initial_posts (§8), scheduled_events: [], hot_topics: [], narrative_direction: ""},
social_graph     {"follow_network": "complete"}          # §6, key baru
twitter_config / reddit_config (dead, §4),
llm_model: Config.LLM_MODEL_NAME, llm_base_url: Config.LLM_BASE_URL,   # tanpa API key
generated_at, generation_reasoning (memuat catatan dead-config §4),
persona_run: {"run_id", "as_of_date", "screening", "generated_at", "grounding"}   # key baru: jejak audit
```
`persona_run` menyimpan `run_id`+`as_of_date` agar Fase 11b/c dapat memverifikasi konsistensi `as_of_date`
(memori requirement backtest 8c: satu `as_of_date` yang sama harus mengalir konsisten) — adapter **tidak**
menghitung ulang tanggal, hanya meneruskan yang ada di `personas_result`.

### Bypass Zep
Tidak memanggil `SimulationManager.prepare_simulation`, `ZepEntityReader`, `OasisProfileGenerator.generate_profiles_from_entities`,
maupun `SimulationConfigGenerator.generate_config`. Impor `simulation_runner.py` yang menarik `ZepGraphMemoryManager`
di level modul tetap terjadi (tidak diubah) tetapi tidak dipanggil selama `enable_graph_memory_update=False`.
Penulisan file **tidak** memakai `OasisProfileGenerator._save_*` karena writer itu menduplikasi bio ke `user_char`
(`bio + " " + persona`), memotong `bio[:150]` sembarang, dan memberi default `country="中国"`; adapter menulis kolom/kunci
**yang sama** (kompatibel dengan `SimulationManager.get_profiles`/frontend) dengan aturan §1–§3.

---

## 8. `initial_posts` — sumber konten

### Koreksi asumsi (temuan A)
Metadata run persona **tidak berisi headline**: `generate_personas` menyimpan `article_ids` saja. Ticker dan
headline hilang, dan meresolusi `article_id` → headline tanpa data tambahan berarti memanggil ulang
`get_news_data` untuk ≤20 ticker (cache-hit untuk `as_of_date` historis karena tanpa TTL; **fetch live ulang
setelah 30 menit** untuk `as_of_date=None`) plus menebak ticker mana yang memuat artikel itu.

**Rekomendasi:** perubahan **aditif kecil di Fase 10** (dikerjakan di fase implementasi, bukan di dokumen ini):
`generate_personas` juga menyimpan `sample_articles` = `[{article_id, ticker, headline, publisher, published_at}]`
(data yang sudah ada di variabel `sample`, tinggal ditulis) — satu kolom `sample_json` di `persona_runs`
(`ALTER TABLE … ADD COLUMN`, pola migrasi yang sudah ada di `_connect`) dan satu key di dict hasil.
Tidak mengubah perilaku generasi, hanya menyimpan lebih banyak. Ini juga mempertahankan prinsip
"tidak fetch ulang" dan konsistensi `as_of_date` (artikel yang tersimpan sudah lolos leakage guard `get_news_data`).
Run yang disimpan sebelum perubahan (tanpa `sample_json`) diperlakukan sebagai "tanpa headline" (fallback di bawah).

### Aturan
- **Jumlah: 4** (maks 4, kurang bila artikel tidak cukup). Alasan: (i) tema berita sample Fase 10 biasanya beberapa
  klaster berbeda (Skenario A: chip AI, stress test bank, minyak, farmasi = 4) — 4 post memberi 4 topik awal tanpa
  satu tema mendominasi; (ii) temuan C membatasi **satu post per agent**, dan 4 dari 8 berarti separuh agent
  menjadi *pemulai* sedangkan separuh lain *bereaksi* — komposisi debat yang sehat; (iii) round hanya 10, jadi
  konten benih harus cukup kaya tapi tidak membanjiri feed Twitter (`refresh_rec_post_count=2`, `max_rec_post_len=2`).
- **Pilih artikel** (deterministik): dari `sample_articles`, urut `published_at` menurun (tie: `article_id` naik);
  ambil artikel pertama per **ticker berbeda** sampai 4.
- **Pilih poster:** urutkan agent `(-influence_weight, agent_id)`; artikel ke-k → agent ke-k dari urutan itu (k=0..3).
  Jadi 4 persona dengan `influence_weight` tertinggi masing-masing menerbitkan 1 post (tidak ada penimpaan, temuan C).
  `agent_id` sebagai tie-breaker menjaga determinisme.
- **Konten:** verbatim, **tanpa LLM** (tidak ada panggilan tambahan, tidak ada risiko mengarang fakta):
  `"{headline} ${ticker} — via {publisher} ({YYYY-MM-DD})"`. Contoh:
  `"Nvidia extends rally as AI chip demand outpaces supply $NVDA — via Reuters (2024-03-12)"`.
  Tanggal dari `published_at[:10]`. Publisher kosong → `"via unknown"` dihilangkan (`"— via …"` bagian dibuang).
  Suara persona muncul di round 1–10 saat mereka merespons, bukan di post benih.
- **Format PERSIS** (skema `event_config.initial_posts[]`, dibaca `poster_agent_id` & `content` oleh skrip):
```json
{"content": "<string di atas>", "poster_type": "Investor", "poster_agent_id": 1}
```
  `poster_type`: contoh nyata run lama memakai tipe entitas Zep (`"Company"`); pencocokan tipe→agent
  (`_assign_initial_post_agents`) **tidak dipakai** di jalur baru (adapter menentukan `poster_agent_id` sendiri) dan
  tidak ada skrip yang membaca `poster_type`, jadi ia informatif saja = `entity_type` agent (`"Investor"`).
- **Fallback (tanpa headline):** `grounding="none"`, run lama tanpa `sample_json`, atau artikel < 1 → tepat **1**
  initial post statis dari agent `influence_weight` tertinggi:
  `"Market check-in: what are you watching in US equities right now, and what's your read?"`,
  dengan warning `"no_news_seed"` di `warnings`. Bukan mengarang berita; sekadar pemancing diskusi agar round 0
  tidak kosong. Bila reviewer lebih suka `initial_posts: []`, cukup satu baris — simulasi tetap valid (`.get(..., [])`).

Catatan konsistensi `as_of_date`: karena headline berasal dari sample run persona yang sama, tanggalnya sudah
≤ `as_of_date` run itu; adapter tidak menambah logika tanggal dan tidak memanggil `get_news_data`.

---

## 9. Pre-flight check SEBELUM implementasi (daftar, bukan didesain)

Semua **belum diverifikasi** dalam investigasi maupun dokumen ini:

1. **HuggingFace `Twitter/twhin-bert-base`** (jalur Twitter, `recsys_type="twhin-bert"`): apakah tersedia di cache lokal
   (`~/.cache/huggingface/hub/models--Twitter--twhin-bert-base`) atau bisa diunduh (jaringan/`HF_HUB_OFFLINE`)? Tanpa ini
   round pertama Twitter gagal saat `update_rec_table`.
2. **`run_parallel_simulation.py` bisa di-import dan berjalan penuh?** Test barrier yang ada hanya me-monkeypatch `Popen`.
   Uji: smoke run 1 round (`--twitter-only`/`--reddit-only`, config kecil) dengan LLM sungguhan; pastikan tidak ada
   `ImportError` (skrip `sys.exit(1)` bila `camel`/`oasis` gagal), dan bahwa `dotenv` menemukan `.env`.
3. **`./log/` writable di `cwd=sim_dir`** (investigasi §9.3). `oasis/environment/env.py` membuat `./log` via
   `os.makedirs` saat import, tetapi `oasis/social_agent/agent.py` membuka `FileHandler("./log/…")` langsung
   (tanpa `makedirs` sendiri); bergantung pada urutan import. Uji: jalankan dari `sim_dir` baru yang kosong.
4. **Function/tool calling di provider LLM saat ini.** Agent OASIS bekerja lewat tool calling (`camel`). Config
   LLM sekarang (vLLM OpenAI-compatible, dipakai Fase 10) belum diuji untuk tool-calling OASIS di branch ini.
5. **Kompatibilitas endpoint lain dengan `state.json` ber-`project_id=""`/`graph_id=""`** (mis. `get_simulation_history`
   yang membaca project/config) — pastikan tidak error 500 untuk simulasi tanpa project.
6. **Urutan pemrosesan pesan di `Platform.running()`** (aman untuk 56 aksi konkuren, §6 risiko 2) dan
   idempotensi `follow` (risiko 3).
7. **Bukti gating** `social_graph` tidak mengubah log run tanpa key itu (§6 risiko 1), dan keputusan apakah
   `run_twitter_simulation.py`/`run_reddit_simulation.py` ikut dimodifikasi.
8. **Bukti temuan B**: jalankan `get_active_agents_for_round` (fungsi murni, mudah diuji tanpa OASIS) dengan
   `time_config` §4 untuk jam 0–9 dan pastikan mengembalikan 8 agent setiap jam.
9. **Temuan F**: konfirmasi duplikasi aksi round-0 di log round 1 pada skrip existing sebelum memperbaikinya.
10. **Ketersediaan `sample_json`** (temuan A): keputusan reviewer apakah Fase 10 diubah aditif (rekomendasi)
    atau memakai re-resolve `get_news_data`.
11. **Biaya/latensi**: 10 round × 8 agent × 2 platform = 160 panggilan LLM aksi + 1 panggilan enrichment;
    `semaphore=30` per platform. Perlu perkiraan token/waktu.

---

## Contoh lengkap: satu set 3 artefak untuk 1 persona ilustratif

Persona: **"Dex Okonkwo-Hale"** (field sumber di §1). Diasumsikan urutan list = index **1** dari 8, dan
enrichment LLM (ilustratif, bukan output nyata) = `age 27, gender "male", mbti "ESTP", country "United Kingdom",
posting_intensity 5, reactivity 5, optimism 4, influence_level 4`. Dengan I=4 ia peringkat pengaruh ke-2 →
mendapat initial post ke-2.

**1) `twitter_profiles.csv`** (header + baris ini)
```csv
user_id,name,username,user_char,description
1,Dex Okonkwo-Hale,dex_okonkwo_hale,"Dex Okonkwo-Hale is an independent investor who shares views on US stocks and market news on social media. Tagline: Rides whatever is going up and bails the second it cracks. Investment philosophy (momentum chaser): Price is the only truth that pays. … Background: Ex-options desk runner turned full-time retail trader based in London.",Ex-options desk runner turned full-time retail trader based in London.
```
(`user_char` = teks penuh §1; `description` = `short_bio` (72 karakter, < 150, tidak dipotong).)

**2) `reddit_profiles.json`** (elemen index 1)
```json
{
  "user_id": 1,
  "username": "dex_okonkwo_hale",
  "name": "Dex Okonkwo-Hale",
  "bio": "Ex-options desk runner turned full-time retail trader based in London.",
  "persona": "Dex Okonkwo-Hale is an independent investor who shares views on US stocks … Background: Ex-options desk runner turned full-time retail trader based in London.",
  "karma": 1000,
  "created_at": "2026-09-21",
  "age": 27,
  "gender": "male",
  "mbti": "ESTP",
  "country": "United Kingdom"
}
```
(`persona` string identik dengan `user_char`; kelima field wajib OASIS terisi: `persona, mbti, gender, age, country`,
plus `username`, `bio`.)

**3) `simulation_config.json` → `agent_configs[1]`**
```json
{
  "agent_id": 1,
  "entity_uuid": "<uuid5(run_id:1)>",
  "entity_name": "Dex Okonkwo-Hale",
  "entity_type": "Investor",
  "activity_level": 1.0,
  "posts_per_hour": 1.0,
  "comments_per_hour": 2.0,
  "active_hours": [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23],
  "response_delay_min": 1,
  "response_delay_max": 10,
  "sentiment_bias": 0.25,
  "stance": "neutral",
  "influence_weight": 1.8
}
```
Bagian yang **hidup**: `agent_id`, `entity_name`, `activity_level`, `active_hours`; `influence_weight` dipakai adapter
(§8). Sisanya **dead config** (§4).

`event_config.initial_posts` (entri ke-2 dari 4, ilustratif; poster = agent ini):
```json
{"content": "Nvidia extends rally as AI chip demand outpaces supply $NVDA — via Reuters (2024-03-12)",
 "poster_type": "Investor", "poster_agent_id": 1}
```
`time_config` §4 (5 jam × 30 menit = 10 round; `peak/off_peak/morning/work_hours` = `[]`, multiplier 1.0,
`agents_per_hour_min = max = 8`); `social_graph` `{"follow_network": "complete"}`.

---

## Alur ringkas end-to-end

```
get_or_generate_personas(as_of_date, sectors, tiers)          [Fase 10, TIDAK diubah*]
        │  personas[8] + run_id + as_of_date + sample_articles*      (*aditif kecil, §8)
        ▼
build_oasis_artifacts(personas_result)                        [BARU: persona_oasis_adapter.py]
   ├─ derive_usernames / compose_persona_text / short_description        (§1–2, murni)
   ├─ get_or_generate_enrichment(run_id, personas)   ← 1 panggilan LLM batch, di-cache per run_id (§3)
   │       └─ demografis Reddit + 4 rating ordinal (retry 3×, temp 1.0/0.9/0.8; gagal → STOP, tanpa file)
   ├─ map_activity(): rating → dead-config + influence_weight; activity_level=1.0; active_hours=24  (§4)
   ├─ build_time_config(): 5 jam × 30 mnt = 10 round, multiplier dinetralkan                        (§4–5)
   ├─ build_event_config(): 4 initial_posts dari headline tersimpan, poster = 4 influence tertinggi (§8)
   └─ tulis atomik ke uploads/simulations/<sim_id>/:
        twitter_profiles.csv → reddit_profiles.json → simulation_config.json → state.json(last)     (§7)
        ▼
SimulationRunner.start_simulation(sim_id, platform="parallel")   [existing, TIDAK diubah;
        │   tanpa max_rounds]                                     lewat API /start bila state.json ada]
        ▼
run_parallel_simulation.py  (twitter ‖ reddit)               [DIUBAH kelak, tergating "social_graph"]
   env.reset()  → agent sign_up
   round 0:  [BARU] follow complete graph: 8×7=56 ManualAction(FOLLOW) + add_edge   (§6)
             initial_posts (4 agent berbeda)
   round 1…10: LLMAction untuk 8 agent (semua aktif, karena 24 jam + 1.0 + multiplier netral)
        ▼
actions.jsonl (round 0..10), run_state.json → completed 10/10
```

---

## Ringkasan keputusan yang perlu persetujuan reviewer

1. Demografis: **LLM batch** (+4 rating ordinal di panggilan yang sama), di-cache per `run_id`; gagal 3× → berhenti tanpa fallback (§3).
2. `activity_level = 1.0` + netralisasi `time_config` (peak/off-peak kosong, multiplier 1.0, `agents_per_hour` = 8) (§4).
3. **5 jam × 30 menit** (bukan 10 × 60) agar log `action_logger` cocok = 10 (§5).
4. Follow-network **digating** oleh `social_graph.follow_network="complete"`; sisip antara `log_round_start(0,0)` dan blok `initial_posts`; majukan `last_rowid` (§6).
5. Adapter menulis **4 file** (bukan 3): `state.json` wajib untuk jalur API `/start` (§7).
6. Fase 10 perlu perubahan **aditif** menyimpan `sample_articles` agar headline tersedia tanpa fetch ulang (§8); jika tidak disetujui, fallback ke re-resolve atau 1 post statis.
7. `initial_posts` **4 post dari 4 agent berbeda** (bukan semua dari agent pengaruh tertinggi) karena penimpaan per-agent di skrip (§8).
8. `stance="neutral"` statis untuk semua persona (dead config, sejalan dengan desain Fase 10 tanpa stance) (§4).

**Tidak ada implementasi setelah dokumen ini — menunggu review.**
