# Fase 11a — Verifikasi Pre-flight (sebelum implementasi adapter)

Tanggal: 2026-09-21. Referensi: `docs/design/fase11a_oasis_adapter.md` §9.
Status: **verifikasi selesai.** Tidak ada file di `backend/` yang diubah, tidak ada commit. Semua script/artefak
scratch dibuat di direktori sementara di luar repo dan **sudah dihapus** (`git status` hanya menampilkan
dua dokumen 11a yang memang belum di-commit).

Lingkungan: `backend/.venv` (Python 3.11, `camel-oasis 0.2.5`, `camel-ai 0.2.78`, `torch 2.9.1`,
`transformers 4.57.3`). LLM: `Qwen/Qwen2.5-7B-Instruct-AWQ` di RunPod vLLM
(`https://api.runpod.ai/v2/nr0mbhbrdv1c40/openai/v1`, dari `.env`; API key tidak ditampilkan).

## Ringkasan status

| # | Item | Status |
|---|---|---|
| 1 | Tool-calling OASIS dengan endpoint LLM sekarang | **VERIFIED OK** (catatan: latensi cold start 2.5–3.7 menit) |
| 2 | `run_parallel_simulation.py` import + run penuh | **VERIFIED OK** |
| 3 | HuggingFace `twhin-bert-base` | **VERIFIED OK** (catatan: jangan set `HF_HUB_OFFLINE=1`) |
| 4 | `./log/` di `cwd` | **VERIFIED OK** (dibuat otomatis; gagal hanya bila `cwd` read-only) |
| 5 | `get_active_agents_for_round` = 8 agent per round | **VERIFIED OK** (200/200 trial) |
| 6 | Duplikasi aksi round-0 → round-1 (temuan F) | **VERIFIED — bug nyata** (dikonfirmasi di kedua platform) |
| 7 | Konsumen `sandbox_clock.time_step` | **Ada konsumen; dampak 1 tick ekstra: tidak ada efek relatif** (detail di bawah) |
| 8 | Endpoint pembaca `state.json` dengan `project_id/graph_id` kosong | **VERIFIED OK untuk jalur Fase 11; 2 kelompok endpoint (bukan jalur 11) akan error** |

**Tidak ada showstopper.** Detail dan bukti per item di bawah.

---

## Item 1 — Tool-calling OASIS dengan endpoint RunPod Qwen2.5-7B-AWQ — VERIFIED OK

**Metode.** Harness scratch memakai `create_model(...)` yang diimpor dari `run_parallel_simulation.py` itu sendiri
(bukan model buatan sendiri), lalu `generate_twitter_agent_graph(... available_actions=TWITTER_ACTIONS)` dengan
3 agent dummy (`ann_investor`, `bo_trader`, `cy_macro`), `oasis.make(TWITTER)`, `env.reset()`, satu `ManualAction`
seed post, lalu tiap agent memanggil `perform_action_by_llm()`.

**Bukti.**
```
MODEL: OpenAIModel Qwen/Qwen2.5-7B-Instruct-AWQ
agents: [(0,'ann_investor'),(1,'bo_trader'),(2,'cy_macro')]  tools: ['create_post','like_post','repost','quote_post','do_nothing','follow']
... Agent 0 performed action: create_post with args: {'content': "I'm keeping an eye on tech giants. CAAT stocks show resilience but remain volatile. #ValueInvesting #Tech"}
... Agent 1 performed action: create_post with args: {'content': "BREAKOUT ALERT! THE MARKET IS ON FIRE! BUY NOW BEFORE IT'S TOO LATE!!! 🚀🔥"}
... Agent 2 performed action: create_post with args: {'content': "Another rally? Let's question this again. #MacroSkeptic #CyMacro"}
AGENT 0/1/2: OK   (info.termination_reasons: ['tool_calls'], usage ~1020 prompt / ~40 completion tokens)
TRACE: (0,'create_post',...post_id 2), (1,'create_post',...post_id 3), (2,'create_post',...post_id 4)
```
Ketiga agent menghasilkan **tool call valid** yang dieksekusi platform dan tercatat di tabel `trace`. Tidak ada error
parsing tool call. Kualitas konten sesuai persona (Ann sober, Bo ALL-CAPS + emoji, Cy skeptis). Run penuh item 2 juga
menghasilkan `QUOTE_POST`, `REPOST`, `CREATE_COMMENT`, `DISLIKE_POST` — jadi bukan hanya `create_post`.

**Catatan (bukan kegagalan):**
- **Latensi cold start yang besar.** Panggilan LLM pertama di sesi harness memakan **±147 detik** (19:39:58 → 19:42:25),
  agent berikutnya ±1.5 detik. Di run item 2 round 1 memakan ±3.7 menit (loop simulasi 235 detik untuk 2 round),
  sedangkan round 2 hanya beberapa detik. Konsisten dengan worker serverless RunPod yang di-scale-to-zero.
  Ini bukan bug, tapi **run 10 round bisa terasa lambat di awal** dan tidak ada timeout/retry khusus di sisi skrip —
  layak dipertimbangkan (mis. panggilan warm-up sebelum `start_simulation`).
- Satu baris `ERROR - list index out of range` (`social.twitter`) muncul pada `update_rec_table` **saat belum ada post**
  (tabel kosong); ditangkap `try/except` di `platform.py` ("If no post in the platform, skip updating the rec table"),
  tidak fatal.

---

## Item 2 — `run_parallel_simulation.py` bisa di-import dan berjalan penuh — VERIFIED OK

**Import.** `importlib` dari `cwd` baru yang kosong: `IMPORT OK in 7.0s`; `run_twitter_simulation`,
`run_reddit_simulation`, `get_active_agents_for_round`, `create_model` semuanya tersedia; `.env` termuat
(`已加载环境配置: .../MiroFish/.env`). Tidak ada `ImportError` (camel/oasis/dotenv).

**Run penuh** — bukan hanya `run_twitter_simulation`, tetapi **`main()` lengkap** lewat subprocess persis seperti
`SimulationRunner` (`python run_parallel_simulation.py --config <cfg> --no-wait`, `PYTHONUTF8=1`), dari direktori kerja baru
tanpa artefak lama, **dua platform paralel**, config buatan tangan: 3 agent, `total_simulation_hours=1`,
`minutes_per_round=30` (=2 round), `time_config` dinetralkan seperti desain §4, `activity_level=1.0`,
`active_hours=24 jam`, 1 `initial_post` (poster agent 1), `twitter_profiles.csv` + `reddit_profiles.json` bentuk desain
(termasuk `age/gender/mbti/country`). Tanpa follow-network.

**Hasil:** `EXIT CODE: 0`; `simulation.log` tanpa `error/traceback/exception/warn`;
`[Twitter] 模拟循环完成! 耗时: 235.2秒, 总动作: 8`, `[Reddit] 模拟循环完成! 耗时: 232.6秒, 总动作: 8`.

`twitter/actions.jsonl` (ringkas):
```
simulation_start total_rounds=2 agents_count=3
round 0: agent 1 CREATE_POST "Nvidia extends rally as AI chip demand outpaces supply $NVDA — via Reuters (2024-03-12)"   round_end actions_count=1
round 1: agent 1 CREATE_POST (Nvidia … ← lihat item 6)  | agent 2 QUOTE_POST quoted_id=1 | agent 1 CREATE_POST "NVDA BREAKOUT! THE MOMENTUM IS STRONG …" | agent 0 QUOTE_POST quoted_id=1     round_end actions_count=4
round 2: agent 1 REPOST | agent 2 CREATE_POST | agent 0 CREATE_POST     round_end actions_count=3
simulation_end total_rounds=2 total_actions=8
```
`reddit/actions.jsonl`: round 1 → `CREATE_POST`(dup, lihat item 6), `DISLIKE_POST`, `CREATE_COMMENT`, `CREATE_POST`; round 2 →
3× `CREATE_COMMENT`; `simulation_end total_rounds=2`. Agent 0/1/2 aktif **di kedua round** (3 dari 3).
Log konsol menampilkan profil Reddit terbaca benar: `{'user_profile': 'Ann is a cautious value investor…', 'mbti': 'ISTJ', 'gender': 'female', 'age': 52, 'country': 'Norway'}`.
Berkas yang muncul di direktori kerja: `twitter_simulation.db`, `reddit_simulation.db`, `twitter/`, `reddit/`, `simulation.log`
(**tidak ada** `log/`, lihat item 4).

Bukti tambahan bahwa nama/format file input valid untuk kontrak desain §7: `twitter_profiles.csv` (5 kolom), `reddit_profiles.json`
(dengan `persona,mbti,gender,age,country,username,bio`), `simulation_config.json` diterima tanpa penyesuaian.

**Catatan:** ini menguji fungsi skrip dan `main()`, **bukan** `SimulationRunner.start_simulation`/monitor thread (test barrier yang ada
sudah meng-cover state machine-nya dengan `Popen` di-monkeypatch). Jalur `SimulationRunner` → skrip identik dengan cara subprocess di atas.

---

## Item 3 — `Twitter/twhin-bert-base` — VERIFIED OK

- Cache lokal ada: `~/.cache/huggingface/hub/models--Twitter--twhin-bert-base` (1,1 GB; `config.json`,
  `model.safetensors`, `tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json`).
- Jaringan ke HuggingFace tersedia (`curl … config.json` → HTTP 307).
- Pemuatan + forward pass: `local_files_only=True` → OK (`hidden (1, 6, 768)`, 2.1 dtk); mode default (online diizinkan) → OK.
- **Terbukti dipakai di run nyata (item 2):** tabel `rec` Twitter berisi 6 baris (`(0,2),(0,4),(1,3),(1,4),(2,2),(2,4)`), yaitu keluaran
  `rec_sys_personalized_twh` — bukan error yang tertelan.

**Catatan/jebakan:** `HF_HUB_OFFLINE=1` **memecahkan** `AutoTokenizer.from_pretrained("Twitter/twhin-bert-base")`
(`OfflineModeIsEnabled` dari `huggingface_hub/hf_api.py:model_info`; transformers 4.57.3 memanggil `model_info` untuk tokenizer),
sedangkan `AutoModel` OK. Jadi jangan menyetel `HF_HUB_OFFLINE=1` di environment runner. Ketergantungan pada ketersediaan jaringan
HuggingFace saat tokenizer dimuat pertama kali **tidak diuji dalam kondisi jaringan mati** (hanya diuji dengan `HF_HUB_OFFLINE=1`
dan dengan `local_files_only=True` yang lolos). Opsi Reddit-only **tidak diperlukan**.

---

## Item 4 — `./log/` di `cwd` — VERIFIED OK

Dari direktori kosong baru, untuk tiga bentuk import (`import oasis.social_agent.agent`,
`from oasis.social_agent.agent import SocialAgent`, `import oasis`): `./log/` **dibuat otomatis** dan berisi
`social.twitter-*.log`, `social.agent-*.log`, `oasis-*.log`, `table-*.log`.

**Urutan yang menyebabkan sukses:** import paket `oasis` selalu menjalankan `oasis/__init__` lebih dulu, yang memuat
`oasis.environment` → `oasis.social_platform.platform` sebelum modul `oasis.social_agent.agent`. Yang membuat direktori adalah
`social_platform/platform.py:39` (`os.makedirs(log_dir)`, lalu `FileHandler("./log/social.twitter-…")`) dan
`environment/env.py:32` (`os.makedirs`). `social_agent/agent.py:44` sendiri **tidak** memanggil `makedirs`, tapi
selalu sudah aman karena dua modul di atas dimuat lebih dulu (log `social.twitter` bertimestamp lebih awal dari `social.agent`).
Adapter **tidak perlu** `mkdir` manual.

**Gagal hanya bila `cwd` tidak bisa ditulis:** `chmod 555` pada `cwd` → `PermissionError: [Errno 13] Permission denied: './log'`
saat `import oasis`. `SimulationRunner` menyetel `cwd=sim_dir` (dibuat adapter), jadi cukup memastikan `sim_dir` writable.

**Temuan sampingan (tidak berbahaya):** `main()` memanggil `init_logging_for_simulation`, yang `rmtree` `sim_dir/log`
**setelah** `oasis` sudah di-import; hasil akhir run item 2: tidak ada `log/`. File log OASIS dibuka sebagai handle lalu direktorinya dihapus
(handle tetap berfungsi, file tak terlihat). Disengaja upstream ("OASIS 的日志太冗余"), tidak memengaruhi run.

---

## Item 5 — `get_active_agents_for_round` — VERIFIED OK

Fungsi diimpor langsung dari `run_parallel_simulation.py`, `env` diganti fake (`agent_graph.get_agent(i)` → `"agent{i}"`),
`time_config` **persis §4** (`5 jam`, `30 menit`, semua `peak/off_peak/morning/work_hours = []`, semua multiplier `1.0`,
`agents_per_hour_min = max = 8`), 8 `agent_configs` (`activity_level=1.0`, `active_hours=range(24)`).

```
total_rounds = 10 | runner formula: 10
round: (simulated_hour, jumlah_agent, semua_8_id_lengkap)
0:(0,8,True) 1:(0,8,True) 2:(1,8,True) 3:(1,8,True) 4:(2,8,True) 5:(2,8,True) 6:(3,8,True) 7:(3,8,True) 8:(4,8,True) 9:(4,8,True)
trials with any round <8 agents: 0 / 200
```
Semua 10 round × 200 trial mengembalikan **8 agent dengan id 0..7 lengkap.** (Jam simulasi 0–4 = `(round*30//60)%24`; permintaan
"jam 0–4 sebagai representasi round 0–9" terpenuhi: tiap jam diuji dua kali.)

**Kontrol yang membuktikan temuan B desain (jebakan multiplier off-peak):**
- `peak_hours`/`off_peak_hours` **dihilangkan** dari `time_config`, `agents_per_hour` 8/8 → jumlah agent per jam 0–4 = `[2, 2, 2, 2, 2]`
  (default MiroFish `off_peak_hours=[0..5]`, multiplier `0.3` → `int(8×0.3)=2`).
- `off_peak_hours=[0..5]`, `off_peak_activity_multiplier=0.05` (default dataclass) → `[0, 0, 0, 0, 0]` (**nol agent**).
Jadi klaim §4 benar dan netralisasi eksplisit memang **wajib**. Verifikasi juga: `(5*60)//30 = 10` (skrip) dan
`int(5*60/30) = 10` (runner).

---

## Item 6 — Duplikasi aksi round-0 → round-1 (temuan F) — VERIFIED: bug nyata

Dari run item 2 (yang punya 1 `initial_post`), **kedua platform**:

`twitter/actions.jsonl`
```
round 0  agent 1 CREATE_POST "Nvidia extends rally … $NVDA — via Reuters (2024-03-12)"     ← dilog manual oleh loop initial_posts
round 0  round_end actions_count=1
round 1  round_start
round 1  agent 1 CREATE_POST "Nvidia extends rally … $NVDA — via Reuters (2024-03-12)", "post_i…"   ← DUPLIKAT: dibaca ulang dari tabel trace
round 1  agent 2 QUOTE_POST … | agent 1 CREATE_POST "NVDA BREAKOUT!…" | agent 0 QUOTE_POST …
round 1  round_end actions_count=4
```
`reddit/actions.jsonl` — pola identik (round 0: `CREATE_POST` Nvidia; round 1: `CREATE_POST` Nvidia yang sama muncul lagi, lalu 3 aksi LLM).

Penyebab, terlihat dari DB: `trace` rowid 4 = `create_post` "Nvidia…" dari `ManualAction` round 0 (rowid 1–3 = `sign_up`, difilter).
`last_rowid` tidak dimajukan setelah round 0 → `fetch_new_actions_from_db(…, 0, …)` di round 1 membaca rowid 4 lagi dan mencatatnya di round 1.
Konsekuensi terukur: round 1 mencatat `actions_count=4` padahal LLM hanya melakukan 3 aksi; `total_actions=8` (seharusnya 7) — hitungan di
`run_state.json`/UI ikut menggelembung 1 per initial post.

Bahwa follow-network **akan memperparah** ini (56 baris `follow` di `trace`) juga dikonfirmasi di item 7: `trace` menyimpan 56 baris `follow`.
Rencana perbaikan di desain §6 (memajukan `last_rowid` setelah round 0) sekarang **berlandaskan bukti**, bukan asumsi.

---

## Item 7 — Konsumen `sandbox_clock.time_step` — ADA konsumen; dampak 1 tick ekstra: tidak ada efek relatif

**Konsumen** (grep seluruh `oasis/` dan `backend/`; nol konsumen di `backend/app` dan `backend/scripts`):

| Lokasi | Pemakaian | Platform |
|---|---|---|
| `environment/env.py:198` | `time_step += 1` di akhir **setiap** `env.step()` | hanya Twitter |
| `social_platform/platform.py` (21 tempat, mis. baris 181/223/261/…) | `current_time = self.sandbox_clock.get_time_step()` → dipakai sebagai **`created_at`** (post, comment, like, follow, `trace`, `sign_up`) | Twitter (Reddit memakai `time_transfer(datetime.now(), start_time)` = jam nyata) |
| `platform.py:369` → `recsys.py:419-472` (`rec_sys_personalized_twh`) | `date_score = log((271.8 - (current_time - int(post['created_at']))) / 100)` = skor **kesegaran** post di feed | Twitter (`twhin-bert`) |
| `scripts/*_simulation.py` (interview) | `ORDER BY created_at DESC` pada `trace` | Twitter & Reddit (hanya pengurutan) |

**Dampak 1 tick ekstra dari `env.step()` follow di round 0** — diuji empiris dengan harness scratch (8 agent, Twitter, tanpa LLM;
`ManualAction(FOLLOW)` untuk semua 56 pasangan dalam **satu** `env.step`), dibandingkan skenario tanpa follow-step:

```
[nofollow]   post created_at: [(1,'seed post',0), (2,'round1 post',1)]
[withfollow] time_step: reset=0 → setelah follow step=1
[withfollow] post created_at: seed=2, round1=3   (harness memanggil follow step 2× untuk uji idempoten → +2; satu kali → +1)
```
- **Stempel absolut bergeser +1** (satu step follow) untuk semua post/komentar/trace setelahnya; baris `follow` sendiri distempel `0`.
- **Skor kesegaran tidak berubah:** rumus memakai `current_time − created_at`. Post seed distempel `t` dan feed round 1 dihitung pada `t+1`
  dengan/tanpa follow-step → selisih (umur) **identik**; hanya nilai absolut yang bergeser seragam. Urutan feed tidak terpengaruh.
- Reddit: tidak terpengaruh (jam nyata; `time_step` tidak dipakai).
- Batas tersirat rumus recency: `271.8 − umur > 0` (komentar kode: "maximum of 90 time steps"). Sesi 10 round + 1–2 step seeding ≈ 12 tick ≪ 90 → aman.
- Perlu diketahui: `time_step` bertambah **per `env.step()`**, bukan per round — round 0 punya 2 step (follow + initial posts) → selisih 1 tick
  hanya di penomoran, tidak mengubah semantik round di skrip (yang berbasis `round_num`).

Jadi: **ada** konsumen di luar aktivasi round (cap `created_at` + recency recsys Twitter), tetapi 1 tick ekstra **tidak berdampak** pada perilaku
relatif; hanya penomoran absolut `created_at` di DB. (Keputusan perbaikan, bila dianggap perlu, tidak diambil di sini.)

**Bukti sampingan untuk risiko §6 desain (pre-flight #6/#3 lama) dari harness yang sama:**
- 56 aksi FOLLOW dalam satu `env.step`: `follow rows: 56 | distinct pairs: 56 | self-follows: 0`, `num_followers = [7,7,7,7,7,7,7,7]`,
  `num_followings = [7,7,7,7,7,7,7,7]` — **tidak ada update hilang** (platform memproses pesan channel berurutan), selesai dalam **1.14 dtk**.
- Mengulang follow-step yang sama: **tidak melempar error dan tidak menambah baris** (idempoten; tetap 56).
- `trace` mencatat **56 baris `follow`** (relevan untuk item 6).

---

## Item 8 — Endpoint pembaca `state.json` dengan `project_id=""`/`graph_id=""` — OK untuk jalur Fase 11; ada endpoint lain yang akan error

**Metode.** Flask `test_client` (`create_app()`), `Config.OASIS_SIMULATION_DATA_DIR` / `SimulationManager.SIMULATION_DATA_DIR` /
`SimulationRunner.RUN_STATE_DIR` diarahkan ke direktori sementara (bukan `backend/uploads`), sebuah simulasi dengan `state.json` persis §7 desain
(`status:"ready"`, `project_id:""`, `graph_id:""`, `config_generated:true`) + 3 artefak. `ProjectManager.get_project("")` → `None` (tidak raise).

| Endpoint | Hasil |
|---|---|
| `GET /api/simulation/history` (yang paling berisiko: memanggil `ProjectManager.get_project(sim.project_id)`) | **200** (`files: []`) |
| `GET /api/simulation/list` | 200 |
| `GET /api/simulation/<id>` | 200 |
| `GET /api/simulation/<id>/profiles` , `/profiles/realtime` | 200 / 200 |
| `GET /api/simulation/<id>/config` , `/config/realtime` | 200 / 200 |
| `GET /api/simulation/<id>/run-status` , `/run-status/detail` | 200 / 200 |
| `POST /api/simulation/prepare/status` | 200 (`already_prepared: true`) |
| `POST /api/simulation/prepare` (tanpa `force_regenerate`) | 200 (`already_prepared: true`, jalur Zep dilewati) |

**Endpoint yang akan gagal / berisiko** (dibaca dari kode; tidak dijalankan karena di luar jalur Fase 11 dan butuh Zep/LLM):
- `POST /api/simulation/prepare` bila **belum** "prepared" atau `force_regenerate=true` → `simulation.py:479-483`
  `ProjectManager.get_project(state.project_id)` → `None` → **404** `projectNotFound` (dan selanjutnya jalur Zep).
- `POST /api/simulation/start` dengan `enable_graph_memory_update=true` → `simulation.py:1686-1688` → **400** (butuh `graph_id` dari project).
  Dengan `false` (default) tidak terpengaruh.
- **Report/Interaction (`app/api/report.py:120-124`, `:647-651`)** → `get_project(state.project_id)` → **404** `projectNotFound`;
  jalur laporan berbasis Zep (`report_agent` memakai `graph_id`) **tidak bisa dipakai** untuk simulasi hasil adapter.
  Ini bukan bagian Fase 11, tetapi UI lama (halaman Report/Interaction) tidak akan berfungsi untuk simulasi tersebut.

---

## Kesimpulan akhir

**Implementasi Fase 11a (adapter) dan modifikasi `run_parallel_simulation.py` (follow-network) AMAN untuk dimulai.**
Tidak ada item yang memaksa revisi desain.

- Item yang semula berpotensi showstopper (**1 tool-calling, 2 run penuh, 3 twhin-bert**) semuanya lolos dengan bukti run nyata
  di kedua platform dari `cwd` bersih (`exit 0`, tanpa error; 8 aksi/platform/2 round; tool call valid; recsys `twhin-bert` menghasilkan tabel `rec`).
- Item 5 membuktikan konfigurasi §4 menghasilkan 8/8 agent di setiap round, dan kontrolnya membuktikan jebakan multiplier off-peak itu **nyata**.
- Item 6 memastikan bug duplikasi log **nyata** di kedua platform — perbaikan `last_rowid` di §6 desain valid dan sebaiknya diikutsertakan.
- Item 7: 1 tick ekstra hanya menggeser stempel absolut; tidak mengubah perilaku feed. Follow-step 56 aksi terbukti tanpa update hilang dan idempoten.

**Catatan/pekerjaan tambahan yang direkomendasikan (bukan pemblokir):**
1. **Latensi cold start RunPod (2.5–3.7 menit)** pada panggilan LLM pertama; tidak ada timeout/retry/warm-up di skrip. Pertimbangkan panggilan pemanasan ringan
   sebelum `start_simulation` atau ekspektasi durasi run pertama.
2. **Jangan set `HF_HUB_OFFLINE=1`** di environment runner (memecahkan tokenizer `twhin-bert` di transformers 4.57.3).
3. Untuk simulasi hasil adapter, **halaman/endpoint Report & Interaction lama tidak akan berfungsi** (butuh project+Zep graph) — perlu diketahui bila UI lama
   dipakai; endpoint status/profil/config/history aman.
4. Konfirmasi tambahan belum dilakukan: perilaku tokenizer `twhin-bert` bila **jaringan benar-benar mati** dan cache ada tanpa `HF_HUB_OFFLINE`
   (hanya diuji dengan `local_files_only=True` dan `HF_HUB_OFFLINE=1`); dan `SimulationRunner.start_simulation` + monitor thread dengan skrip sungguhan
   (yang diuji: skrip langsung, sama dengan yang dipanggil runner).

Tidak ada implementasi Fase 11a dimulai. Menunggu review.
