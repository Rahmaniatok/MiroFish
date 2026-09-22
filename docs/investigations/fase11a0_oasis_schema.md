# Fase 11a-0 — Investigasi Skema Input OASIS (`simulation_runner.py`)

Status: **investigasi selesai, tidak ada file kode yang diubah/dibuat.** Menunggu review sebelum desain 11a.

Semua kutipan di bawah diambil langsung dari kode (bukan parafrase). Library OASIS yang
sebenarnya dieksekusi (`camel-oasis 0.2.5`) sudah terpasang di `backend/.venv`, jadi kontrak
input yang **benar-benar dibaca** bisa dikonfirmasi dari sumbernya, bukan hanya dari kode MiroFish.
Satu sanity check: `import oasis` di `backend/.venv` berhasil (`0.2.5`) — tidak ada yang diinstall.

---

## Ringkasan temuan (baca ini dulu)

1. `simulation_runner.py` **masih ada**, utuh, dan pernah **berjalan sukses end-to-end** (ada run
   sungguhan `sim_11bce4ea88a8`: `runner_status=completed`, 10/10 round, 2 platform).
2. `simulation_runner.py` sendiri **tidak menerima persona**. Ia hanya `subprocess.Popen` skrip
   `backend/scripts/run_*_simulation.py` dengan `--config simulation_config.json`. Input persona
   yang sebenarnya adalah **dua file profil per platform** (`twitter_profiles.csv`,
   `reddit_profiles.json`) + **`simulation_config.json`** — tiga artefak, bukan satu skema.
3. **Istilah "response latency / engagement frequency / sentiment baseline / influence weight"
   adalah istilah longgar.** Nama asli ada di `AgentActivityConfig`, dan **hanya 2 dari field
   aktivitas itu yang benar-benar dibaca oleh skrip runner** (`activity_level`, `active_hours`).
   Sisanya di-generate tapi **tidak pernah dikonsumsi** (lihat §2.C).
4. **Follow-network complete graph TIDAK ada** di kode existing dan **tidak otomatis terbentuk**.
   Run nyata punya tabel `follow` = 0 baris. Harus dibangun dari nol.
5. Satu skema persona (teks) dipakai untuk dua platform; yang beda hanya **format file** dan
   **field demografis wajib Reddit** (`age/gender/mbti/country`). Perilaku beda platform ada di
   level environment/action-list, bukan persona.
6. **OASIS tidak punya konsep "round".** Round = iterasi loop di skrip MiroFish; OASIS hanya punya
   `env.step()`.
7. Ada beberapa **temuan yang mengubah asumsi** (§9): `oasis_profile_generator.py` sudah
   dimodifikasi Fase 3 (bukan generator OASIS lagi untuk jalur simulasi), `user_name` NULL di DB,
   `twhin-bert` butuh download model, dsb.

---

## 0. File yang dibaca (path lengkap)

Repo (`/home/rahmania/Documents/GitHub/MiroFish`):
- `backend/app/services/simulation_runner.py` (2099 baris; dibaca baris 1–730 + grep sisanya)
- `backend/app/services/simulation_config_generator.py` (993 baris; dibaca dataclass §1–200 + grep prompt/rule-based)
- `backend/app/services/oasis_profile_generator.py` (2150 baris; dibaca `OasisAgentProfile`, `save_profiles`, `_save_twitter_csv`, `_save_reddit_json`, `_normalize_gender`)
- `backend/app/services/simulation_manager.py` (grep alur prepare/profil)
- `backend/app/services/persona_generator.py` (`InvestorPersona`, konstanta)
- `backend/scripts/run_parallel_simulation.py` (1699 baris; dibaca import, action list, `create_model`, `get_active_agents_for_round`, `run_twitter_simulation`, `run_reddit_simulation`, `main`)
- `backend/scripts/run_twitter_simulation.py`, `run_reddit_simulation.py` (baris 476–510 + grep — logika aktivasi identik)
- `backend/scripts/action_logger.py` (grep konsep round)
- `backend/scripts/test_profile_format.py`
- `backend/tests/test_platform_profiles.py`, `test_profile_field_normalization.py`, `test_simulation_prepare_failure.py`, `test_zep_simulation_barrier.py`
- `backend/requirements.txt`, `backend/pyproject.toml`
- `PHASES.md`, `docs/design/fase10a_persona_generation.md`
- Artefak run lama (gitignored): `backend/uploads/simulations/sim_11bce4ea88a8/{simulation_config.json,twitter_profiles.csv,reddit_profiles.json,run_state.json,twitter_simulation.db,reddit_simulation.db,twitter/actions.jsonl}`

Library pihak ketiga (read-only, `backend/.venv/lib/python3.11/site-packages/oasis/`):
- `__init__.py`, `environment/env.py`, `environment/env_action.py`, `environment/make.py`
- `social_agent/agents_generator.py`, `agent_graph.py`, `agent.py`, `agent_action.py`
- `social_platform/config/user.py`, `platform.py`, `recsys.py`, `typing.py`

---

## 1. Keberadaan `simulation_runner.py`

**Ada, belum dihapus.** Path:

```
backend/app/services/simulation_runner.py      # orchestrator (Flask side)
backend/scripts/run_parallel_simulation.py     # dual-platform, dipakai default (platform="parallel")
backend/scripts/run_twitter_simulation.py      # single-platform
backend/scripts/run_reddit_simulation.py       # single-platform
backend/scripts/action_logger.py               # logger actions.jsonl
backend/app/services/simulation_ipc.py         # IPC interview
backend/app/services/simulation_config_generator.py  # menghasilkan simulation_config.json
```

Peran `simulation_runner.py` — **launcher + monitor**, bukan consumer persona:

```python
# simulation_runner.py:471-483
if platform == "twitter":
    script_name = "run_twitter_simulation.py"
elif platform == "reddit":
    script_name = "run_reddit_simulation.py"
else:
    script_name = "run_parallel_simulation.py"
...
cmd = [
    sys.executable,  # Python解释器
    script_path,
    "--config", config_path,  # 使用完整配置文件路径
]
```

Prasyarat `start_simulation`: `simulation_config.json` harus ada di `uploads/simulations/<sim_id>/`
(`raise ValueError("模拟配置不存在，请先调用 /prepare 接口")`).

`PHASES.md` mengonfirmasi bypass Fase 4: *"bukan simulasi OASIS dua-platform yang lama —
investigasi di fase ini menyimpulkan OASIS tidak menambah nilai untuk 6 persona tetap tanpa
social graph/feed"*. Route/halaman OASIS lama "tidak disentuh".

**Catatan riwayat:** `git log` untuk `scripts/` dan `simulation_config_generator.py` hanya berisi
commit upstream MiroFish (i18n, fix Windows encoding, Zep) — tidak ada commit rebuild yang mengubah
skrip runner. Artinya runner = kode MiroFish original, tidak diadaptasi ke domain investasi.

---

## 2. Skema input per agent

Skema input tersebar di **tiga artefak**. Ketiganya harus konsisten pada **indeks urut agent**
(lihat §2.D).

### 2.A `twitter_profiles.csv` — dibaca oleh `generate_twitter_agent_graph`

Yang **benar-benar dibaca OASIS** (`oasis/social_agent/agents_generator.py`):

```python
async def generate_twitter_agent_graph(profile_path, model=None, available_actions=None) -> AgentGraph:
    agent_info = pd.read_csv(profile_path)
    agent_graph = AgentGraph()
    for agent_id in range(len(agent_info)):
        profile = {"nodes": [], "edges": [], "other_info": {}}
        profile["other_info"]["user_profile"] = agent_info["user_char"][agent_id]

        user_info = UserInfo(
            name=agent_info["username"][agent_id],
            description=agent_info["description"][agent_id],
            profile=profile,
            recsys_type='twitter',
        )
        agent = SocialAgent(agent_id=agent_id, user_info=user_info, model=model,
                            agent_graph=agent_graph, available_actions=available_actions)
        agent_graph.add_agent(agent)
    return agent_graph
```

| Kolom CSV | Status | Tipe | Dibaca? | Fungsi |
|---|---|---|---|---|
| `user_char` | **wajib** (KeyError bila hilang) | string | ya | teks persona → system prompt LLM |
| `username` | **wajib** | string | ya | jadi `UserInfo.name` (nama tampil di system prompt & DB) |
| `description` | **wajib** | string | ya | bio publik (`UserInfo.description`) |
| `name` | ditulis MiroFish, **tidak dibaca** oleh fungsi ini | string | tidak | — |
| `user_id` | ditulis MiroFish, **tidak dibaca** | int | tidak | agent_id = **posisi baris** (`range(len(agent_info))`) |

Yang **ditulis** MiroFish (`oasis_profile_generator.py:2041`):
```python
headers = ['user_id', 'name', 'username', 'user_char', 'description']
```
Contoh nyata (`sim_11bce4ea88a8/twitter_profiles.csv`):
```
user_id,name,username,user_char,description
0,Kurtz,kurtz_484,"Kurtz is a cybersecurity expert who works closely with CrowdStrike and NVIDIA ...",...
```
`user_char` dibangun MiroFish sebagai `f"{bio} {persona}"` (newline diganti spasi).

**Tidak ada validasi rentang eksplisit** di sisi OASIS untuk kolom ini (hanya `pd.read_csv` + indexing).
`test_profile_format.py` (§6) mencantumkan "kolom wajib" yang **berbeda** dari writer sungguhan — stale.

### 2.B `reddit_profiles.json` — dibaca oleh `generate_reddit_agent_graph`

```python
async def generate_reddit_agent_graph(profile_path, model=None, available_actions=None):
    agent_graph = AgentGraph()
    with open(profile_path, "r") as file:
        agent_info = json.load(file)

    async def process_agent(i):
        profile = {"nodes": [], "edges": [], "other_info": {}}
        profile["other_info"]["user_profile"] = agent_info[i]["persona"]
        profile["other_info"]["mbti"] = agent_info[i]["mbti"]
        profile["other_info"]["gender"] = agent_info[i]["gender"]
        profile["other_info"]["age"] = agent_info[i]["age"]
        profile["other_info"]["country"] = agent_info[i]["country"]

        user_info = UserInfo(
            name=agent_info[i]["username"],
            description=agent_info[i]["bio"],
            profile=profile,
            recsys_type="reddit",
        )
        agent = SocialAgent(agent_id=i, ...)
```

| Field JSON | Status | Tipe | Catatan |
|---|---|---|---|
| `persona` | **wajib** | string | system prompt |
| `mbti` | **wajib** | string | dipakai di system prompt Reddit |
| `gender` | **wajib** | string | MiroFish menormalkan ke `male`/`female`/`other` (`_normalize_gender`) — **OASIS sendiri tidak memvalidasi** |
| `age` | **wajib** | int | tidak ada validasi rentang di OASIS; MiroFish default `30` |
| `country` | **wajib** | string | MiroFish default `"中国"` (!) |
| `username` | **wajib** | string | `UserInfo.name` |
| `bio` | **wajib** | string | MiroFish memotong `[:150]` |
| `user_id`, `name`, `karma`, `created_at`, `profession`, `interested_topics` | ditulis MiroFish, **tidak dibaca** | — | agent_id = indeks list |

Kelima field pertama (`persona, mbti, gender, age, country`) berupa akses `[]` langsung → `KeyError`
bila hilang. Ini **satu-satunya perbedaan skema persona antar platform** (§4).

Konsumsi di system prompt Reddit (`social_platform/config/user.py`):
```python
description += (
    f"You are a {self.profile['other_info']['gender']}, "
    f"{self.profile['other_info']['age']} years old, with an MBTI "
    f"personality type of {self.profile['other_info']['mbti']} from "
    f"{self.profile['other_info']['country']}.")
```
Dan system prompt Twitter — **hanya** `name` + `user_profile`, tanpa demografi:
```python
description_string = f"Your have profile: {user_profile}."
description = f"{name_string}\n{description_string}"
```

### 2.C `simulation_config.json` → `agent_configs[]` (`AgentActivityConfig`)

Ini tempat 4 istilah dari percakapan sebelumnya berasal. Kutipan lengkap
(`simulation_config_generator.py:52-81`):

```python
@dataclass
class AgentActivityConfig:
    agent_id: int
    entity_uuid: str
    entity_name: str
    entity_type: str

    # 活跃度配置 (0.0-1.0)
    activity_level: float = 0.5  # 整体活跃度

    # 发言频率（每小时预期发言次数）
    posts_per_hour: float = 1.0
    comments_per_hour: float = 2.0

    # 活跃时间段（24小时制，0-23）
    active_hours: List[int] = field(default_factory=lambda: list(range(8, 23)))

    # 响应速度（对热点事件的反应延迟，单位：模拟分钟）
    response_delay_min: int = 5
    response_delay_max: int = 60

    # 情感倾向 (-1.0到1.0，负面到正面)
    sentiment_bias: float = 0.0

    # 立场（对特定话题的态度）
    stance: str = "neutral"  # supportive, opposing, neutral, observer

    # 影响力权重（决定其发言被其他Agent看到的概率）
    influence_weight: float = 1.0
```

#### Koreksi istilah

| Istilah percakapan lama | Nama asli di kode | Ada persis? | Dikonsumsi runner? |
|---|---|---|---|
| response latency | `response_delay_min` + `response_delay_max` (int, menit simulasi) | **Tidak** — dua field, bukan satu | **TIDAK** |
| engagement frequency | `activity_level` (0.0–1.0) dan/atau `posts_per_hour` / `comments_per_hour` (float) | **Tidak** — ambigu, 3 kandidat | `activity_level` **ya**; `posts_per_hour`/`comments_per_hour` **TIDAK** |
| sentiment baseline | `sentiment_bias` (−1.0..1.0) | **Tidak** — nama asli `sentiment_bias` | **TIDAK** |
| influence weight | `influence_weight` (float) | **Ya, persis** | **TIDAK** oleh runner; hanya dipakai `_assign_initial_post_agents` untuk memilih siapa yang memposting event awal (`sorted(agent_configs, key=lambda a: a.influence_weight, reverse=True)`) |

Ditambah field yang **belum pernah disebut**: `stance` (enum string `supportive/opposing/neutral/observer`
— hanya divalidasi lewat instruksi prompt, bukan kode), `active_hours` (list[int] 0–23),
`entity_uuid`, `entity_type`, `entity_name`.

#### Bukti "tidak dikonsumsi"
`grep` untuk `response_delay|sentiment_bias|influence_weight|posts_per_hour|comments_per_hour|stance|echo_chamber|viral_threshold|recency_weight`
di `backend/scripts/` dan `backend/app/` (di luar `simulation_config_generator.py` sendiri): **nol hasil** yang
terkait `agent_configs` di skrip runner. Field yang dibaca skrip hanya:

```python
# run_parallel_simulation.py:1066-1073 (identik di run_twitter/run_reddit)
for cfg in agent_configs:
    agent_id = cfg.get("agent_id", 0)
    active_hours = cfg.get("active_hours", list(range(8, 23)))
    activity_level = cfg.get("activity_level", 0.5)

    if current_hour not in active_hours:
        continue
    if random.random() < activity_level:
        candidates.append(agent_id)
```
dan `agent_id` + `entity_name` di `get_agent_names_from_config` (untuk label di `actions.jsonl`).

Jadi **kontrak minimal yang benar-benar dibaca** per agent = `{agent_id, entity_name, active_hours, activity_level}`.
Field lain di `AgentActivityConfig` adalah metadata mati (dead config) untuk runner ini. Ini
kemungkinan besar juga berlaku di MiroFish upstream — **tidak dapat dipastikan dari repo ini**, hanya
terbukti bahwa di repo ini tidak ada konsumennya.

Validasi eksplisit di kode: **tidak ada** (tidak ada `clamp`, assert, atau schema check pada
`activity_level`/`sentiment_bias`/dll di sisi konsumen). Rentang hanya tercantum di komentar dan prompt LLM.
Parsing lenient: `cfg.get("x", default)`.

#### Top-level `simulation_config.json` yang dibaca runner
```
time_config.total_simulation_hours  (default 72)
time_config.minutes_per_round       (default 30 di skrip; dataclass default 60)
time_config.agents_per_hour_min/max (default 5/20)
time_config.peak_hours / off_peak_hours + peak_activity_multiplier / off_peak_activity_multiplier
event_config.initial_posts[]        → {content, poster_agent_id, poster_type}
llm_model                           (fallback bila env LLM_MODEL_NAME kosong)
```
`twitter_config`/`reddit_config` (`PlatformConfig`: `recency_weight`, `popularity_weight`,
`relevance_weight`, `viral_threshold`, `echo_chamber_strength`) **di-generate tetapi tidak dibaca
skrip mana pun** (grep nol).

### 2.D Kunci identitas & kontrak indeks

- `agent_id` OASIS = **posisi baris/elemen** di file profil (`SocialAgent(agent_id=i)`).
  `user_id` di file **diabaikan**.
- `simulation_config.agent_configs[i].agent_id` **harus sama** dengan indeks itu (dipakai
  `env.agent_graph.get_agent(agent_id)` dan `initial_posts.poster_agent_id`). Tidak ada verifikasi silang otomatis.
- Agent id di luar range → `except Exception: pass` (di-skip diam-diam, `get_active_agents_for_round`).
- **Quirk `user_name` NULL**: `generate_*_agent_graph` membuat `UserInfo(name=username, ...)` tanpa
  `user_name`; `reset()` → `generate_custom_agents` memanggil
  `sign_up(user_name=agent.user_info.user_name, name=agent.user_info.name, bio=...)`. Terkonfirmasi
  di DB run nyata: `user` row = `(0, None, 'kurtz_484')` → kolom `user_name` NULL, `name` berisi username.
  (Perilaku upstream; tidak merusak run.)

### 2.E Field lain yang **mungkin dibutuhkan** tapi belum pernah disebut

| Item | Sumber | Catatan |
|---|---|---|
| `username` unik per agent | OASIS graph fn | Tidak ada cek unik; dipakai sebagai nama tampil. Persona Fase 10 hanya punya `name`. |
| `age`, `gender`, `mbti`, `country` (Reddit) | `generate_reddit_agent_graph` | **Wajib** (KeyError). Tidak ada di Fase 10. |
| `bio`/`description` terpisah dari persona | kedua platform | Wajib, terpisah dari teks persona |
| `agent_id` berurutan 0..N−1 tanpa lubang | graph fn + config | Harus konsisten di CSV, JSON, config |
| `event_config.initial_posts[]` (`content`, `poster_agent_id`) | runner | Opsional secara teknis (`.get(..., [])`); tanpa ini round 0 kosong |
| `time_config` | runner | Menentukan jumlah round (lihat §5) |
| Env var LLM: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL_NAME` (+ opsional `LLM_BOOST_*`) | `create_model` | `raise ValueError` bila API key kosong. Reddit memakai `use_boost=True` |
| `.env` di root/`backend/` | skrip | dimuat via `dotenv` |
| `./log/` writable di `cwd` | `oasis/social_agent/agent.py`, `environment/env.py` | `logging.FileHandler("./log/social.agent-...")` saat import; runner mengatur `cwd=sim_dir` |
| Model `Twitter/twhin-bert-base` (HuggingFace) | recsys Twitter | Lihat §7 |

---

## 3. Follow-network

**Representasi di OASIS:** dua tempat yang **tidak otomatis sinkron**:

1. **`AgentGraph`** (in-memory, `igraph` directed graph):
```python
def add_edge(self, agent_id_0: int, agent_id_1: int):
    try:
        self.graph.add_edge(agent_id_0, agent_id_1)
    except Exception:
        pass
def get_edges(self) -> list[tuple[int, int]]:   # daftar edge (src, dst)
```
   Format = **daftar edge terarah** (bukan adjacency matrix).
2. **Tabel `follow` di SQLite platform** (`follower_id, followee_id, created_at`) — inilah yang
   **benar-benar dipakai recsys/feed** (`platform.py:288`: `"JOIN follow ON post.user_id = follow.followee_id"`).

**Yang dibaca `generate_*_agent_graph`: tidak ada edge sama sekali.** Fungsi itu hanya `add_agent`.
Satu-satunya jalur OASIS yang memuat follow dari file adalah `generate_agents()` (kolom CSV
`following_agentid_list`, di-parse `ast.literal_eval`) — **tidak dipakai MiroFish** (MiroFish memanggil
`generate_twitter_agent_graph`). CSV MiroFish bahkan tidak punya kolom itu.

**Bukti empiris:** run nyata `sim_11bce4ea88a8` → `follow` = **0 baris** di kedua DB (19 user).

**Contoh/default "semua follow semua": tidak ada** di kode MiroFish. Di library ada padanan kecil
tapi bukan yang dipakai di sini (`generate_controllable_agents`, untuk agent manual, interaktif via `input()`):
```python
for i in range(control_user_num):
    for j in range(control_user_num):
        # controllable agent互相也全部关注
        if i != j:
            await agent.env.action.follow(user_id)
            agent_graph.add_edge(i, j)
```
Pola ini membuktikan mekanisme yang valid: **panggil `follow` action (menulis ke tabel `follow`) dan
`agent_graph.add_edge`**. Untuk agent LLM di MiroFish, jalur setara adalah
`ManualAction(ActionType.FOLLOW, {"followee_id": N})` (signature `agent_action.follow(self, followee_id: int)`)
pada round 0 — sama seperti pola `initial_posts` yang sudah ada. **Ini perlu dibangun dari nol di 11a.**

Catatan: `FOLLOW` masuk `TWITTER_ACTIONS` dan `REDDIT_ACTIONS`, jadi agent LLM juga bisa follow sendiri,
tetapi itu spontan, bukan jaringan yang ditentukan.

---

## 4. Dual-platform: skema persona berbeda atau sama?

**Satu sumber teks persona; dua format file; satu-satunya beda skema = 4 field demografis wajib Reddit.**
Tidak ada field "karakter-limit-awareness" (Twitter) maupun "subreddit-preference" (Reddit) di kode manapun.

| Aspek | Twitter | Reddit | Dimana |
|---|---|---|---|
| Format file | CSV (`user_char, username, description`) | JSON (`persona, username, bio, age, gender, mbti, country`) | `agents_generator.py` |
| Field demografis di system prompt | tidak ada | `gender, age, mbti, country` wajib | `config/user.py` |
| Template system prompt | `"You're a Twitter user"` | `"You're a Reddit user"` | `config/user.py` |
| Recsys | `recsys_type="twhin-bert"`, `refresh_rec_post_count=2`, `max_rec_post_len=2`, `following_post_count=3` | `recsys_type="reddit"`, `allow_self_rating=True`, `show_score=True`, `max_rec_post_len=100`, `refresh_rec_post_count=5` | `environment/env.py` (**hard-coded** di `OasisEnv.__init__`, tidak bisa diubah lewat config MiroFish) |
| Action space | `CREATE_POST, LIKE_POST, REPOST, FOLLOW, DO_NOTHING, QUOTE_POST` | `LIKE/DISLIKE_POST, CREATE_POST, CREATE_COMMENT, LIKE/DISLIKE_COMMENT, SEARCH_POSTS, SEARCH_USER, TREND, REFRESH, DO_NOTHING, FOLLOW, MUTE` | `TWITTER_ACTIONS` / `REDDIT_ACTIONS` di skrip |
| LLM | `create_model(use_boost=False)` | `create_model(use_boost=True)` | skrip |
| Jam simulasi | `sandbox_clock.time_step += 1` per `step` | `time_transfer(datetime.now(), start_time)` | `env.step`, `platform.py` |

Jadi jawabannya: **perilaku beda platform ditentukan di level environment/action-list/recsys, bukan persona.**
Di persona hanya perlu memenuhi field wajib tiap format.

---

## 5. Konsep "round"

**OASIS tidak punya konsep round eksplisit.** Unit terkecil = `OasisEnv.step(actions)`:
```python
async def step(self, actions: dict[SocialAgent, Union[ManualAction, LLMAction, List[...]]]) -> None:
    await self.platform.update_rec_table()
    ...
    await asyncio.gather(*tasks)
    if self.platform_type == DefaultPlatformType.TWITTER:
        self.platform.sandbox_clock.time_step += 1
```
"Round" adalah **loop MiroFish** di skrip:
```python
# run_parallel_simulation.py:1195-1233
total_rounds = (total_hours * 60) // minutes_per_round
if max_rounds is not None and max_rounds > 0:
    total_rounds = min(total_rounds, max_rounds)
for round_num in range(total_rounds):
    simulated_minutes = round_num * minutes_per_round
    simulated_hour = (simulated_minutes // 60) % 24
    active_agents = get_active_agents_for_round(result.env, config, simulated_hour, round_num)
    actions = {agent: LLMAction() for _, agent in active_agents}
    await result.env.step(actions)
```
- Round 0 = fase event awal (`initial_posts` via `ManualAction(CREATE_POST)`); round 1..N = loop.
- Nama di kode: `round_num`, `total_rounds`, `--max-rounds` (CLI), `minutes_per_round`,
  `total_simulation_hours`; log `event_type: "round_start"/"round_end"`.
- **Jumlah round tidak diset langsung** — diturunkan: `total_rounds = total_simulation_hours*60 // minutes_per_round`,
  lalu opsional dipotong `--max-rounds`. Cara "10 rounds": `max_rounds=10` (persis cara run lama
  `sim_11bce4ea88a8`: `total_rounds: 10` di run_state, meski `actions.jsonl` mencatat `total_rounds: 144`
  di event `simulation_start`) atau set `time_config` agar hasil bagi = 10.
- **Kaitan waktu:** setiap round mengaktifkan agent berdasarkan `simulated_hour` (jam 0–23) dan
  `active_hours` agent; default `active_hours = 8..22`. Dengan `minutes_per_round=60`, 10 round = jam 0–9
  → **sebagian besar agent tidak aktif** di round awal (jam 0–7 masuk `off_peak_hours` multiplier 0.05/0.3 dan di luar `active_hours`
  default). Perlu diperhatikan di desain 11a.
- Ketidakkonsistenan kecil: `action_logger.py:98` menulis `total_rounds = total_simulation_hours * 2` (hard-coded, mengasumsikan 30 menit/round) —
  hanya angka log, bukan kontrol.
- Padanan `debate_room.py`: `rounds` di debate_room adalah giliran bicara LLM sinkron dalam satu ruang;
  round OASIS adalah langkah waktu dengan aktivasi probabilistik + recsys + feed. **Tidak setara semantiknya.**

---

## 6. Contoh / fixture / test yang ada

**Tidak ada test pytest yang menjalankan atau memvalidasi input `run_*_simulation.py` / `generate_*_agent_graph`.**

| File | Yang diuji | Berguna sebagai contoh input valid? |
|---|---|---|
| `tests/test_profile_field_normalization.py` | `OasisAgentProfile` → `_save_twitter_csv` / `_save_reddit_json` | **Ya (sebagian)** — memverifikasi bentuk output writer: `twitter["user_char"] == "公开简介 详细, 人设"` |
| `tests/test_platform_profiles.py` | `SimulationManager.get_profiles` (pembacaan) | **Tidak** — fixture memakai kolom `agent_id,name` yang **bukan** skema OASIS |
| `tests/test_zep_simulation_barrier.py` | `SimulationRunner.start_simulation` dengan `Popen` di-monkeypatch | Ada `simulation_config.json` mini (`time_config`) — hanya untuk state machine, bukan input OASIS |
| `tests/test_simulation_prepare_failure.py` | endpoint realtime status gagal | Tidak |
| `scripts/test_profile_format.py` | bukan pytest (ada di `scripts/`, dijalankan manual) | **Stale/menyesatkan** — lihat di bawah |

`scripts/test_profile_format.py` mengklaim kolom wajib Twitter `['user_id','user_name','name','bio','friend_count','follower_count','statuses_count','created_at']`
dan Reddit `['realname','username','bio','persona']`. Keduanya **bertentangan** dengan writer sungguhan dan
library OASIS 0.2.5 (writer: `user_id,name,username,user_char,description`; Reddit: `username`/`name` bukan `realname`,
dan `age/gender/mbti/country` wajib). **Jangan dijadikan referensi kontrak.**

**Sample nyata terbaik = artefak run lama** `backend/uploads/simulations/sim_11bce4ea88a8/` (gitignored, dari pipeline
MiroFish original sebelum rebuild; 19 agent, run `completed` 10/10 round):

`agent_configs[0]`:
```json
{"agent_id": 0, "entity_uuid": "f92a9267-...", "entity_name": "Kurtz", "entity_type": "Person",
 "activity_level": 0.3, "posts_per_hour": 0.5, "comments_per_hour": 0.3,
 "active_hours": [9,10,11,12,13,14,15,16,17],
 "response_delay_min": 60, "response_delay_max": 120,
 "sentiment_bias": 0.0, "stance": "observer", "influence_weight": 2.5}
```
`time_config`:
```json
{"total_simulation_hours": 72, "minutes_per_round": 60, "agents_per_hour_min": 10, "agents_per_hour_max": 11,
 "peak_hours": [19,20,21,22], "peak_activity_multiplier": 1.5,
 "off_peak_hours": [0,1,2,3,4,5], "off_peak_activity_multiplier": 0.05, ...}
```
`event_config.initial_posts[0]`:
```json
{"content": "NVIDIA and CrowdStrike have announced ... #NVIDIA #CrowdStrike #SafeMind",
 "poster_type": "Company", "poster_agent_id": 13}
```
`reddit_profiles.json[0]`: `{"user_id":0,"username":"kurtz_484","name":"Kurtz","bio":"...","persona":"...","age":...,"gender":...,"mbti":...,"country":...}`

`run_state.json`: `runner_status=completed, current_round=10, total_rounds=10, twitter_actions_count=21, reddit_actions_count=15, error=null`.
Ini **satu-satunya bukti** end-to-end bahwa input berbentuk begini valid dan menghasilkan run. Perlu dicatat: run ini terjadi
sebelum rebuild (entity Zep/Person/Company, bukan persona investor).

---

## 7. Dependensi eksternal

| Paket | Status |
|---|---|
| `camel-oasis==0.2.5` | **Sudah ada** di `backend/requirements.txt:22` dan `backend/pyproject.toml:24`; **terpasang** di `backend/.venv` (`import oasis` → `0.2.5`) |
| `camel-ai==0.2.78` | **Sudah ada** di kedua file dependency; terpasang |
| `torch 2.9.1`, `transformers 4.57.3`, `sentence-transformers 3.0.0`, `igraph 0.11.6`, `neo4j 5.23.0`, `pandas 2.2.2` | terpasang di `.venv` (dependensi transitif OASIS; `torch`/`transformers` **tidak** tercantum eksplisit di requirements/pyproject — datang via `camel-oasis`) |
| Model HuggingFace `Twitter/twhin-bert-base` | **Bukan paket, tapi runtime download** — `recsys.py` memanggil `AutoModel.from_pretrained("Twitter/twhin-bert-base")` saat platform Twitter (`recsys_type="twhin-bert"`) pertama kali me-refresh rec table. Butuh jaringan/cache HF. Di run nyata sebelumnya ini tampaknya berhasil (ada 17 post dan 21 aksi Twitter), tetapi **tidak diverifikasi ulang** di environment sekarang. |
| Kredensial LLM (`LLM_API_KEY`) | wajib; skrip `sys.exit(1)` bila `ImportError`, `ValueError` bila key kosong |
| Neo4j server | **tidak diperlukan** (default backend `igraph`) |
| Zep Cloud | `simulation_runner.py` meng-import `ZepGraphMemoryManager`/`utils.zep` di level modul dan `simulation_manager.py` mem-prepare dari `ZepEntityReader` + `graph_id`. Jalur prepare **bergantung pada Zep**; jalur run tidak, kecuali `enable_graph_memory_update=True`. |

Tidak ada yang diinstall dalam investigasi ini.

---

## 8. Celah: `InvestorPersona` (Fase 10) vs kebutuhan OASIS

`InvestorPersona` (`persona_generator.py:64-75`):
```python
name: str
tagline: str
investment_philosophy: str
philosophy_label: str
biases: List[str]
personality: str
communication_style: str
edge_vs_others: str
short_bio: Optional[str] = None
```
Tidak punya `agent_id`, `username`, `age`, `gender`, `mbti`, `country`, atau field aktivitas.

Kolom "Sumber jika belum ada" berisi **opsi yang teridentifikasi dari kode**, bukan keputusan desain.

| Field OASIS | Artefak | Wajib? | Padanan di Fase 10? | Field Fase 10 | Bila tidak ada: sumber yang mungkin |
|---|---|---|---|---|---|
| `user_char` / `persona` (teks persona) | CSV / JSON | **wajib** | Ada (komposit) | `investment_philosophy` + `personality` + `communication_style` + `biases` + `edge_vs_others` (komposisi teks, format bebas) | — |
| `description` / `bio` | CSV / JSON | **wajib** | Ada | `short_bio` (opsional → bisa None) atau `tagline` sebagai fallback | derivasi dari `tagline` |
| `name` | CSV (tak dibaca) / JSON (tak dibaca) | tidak dibaca OASIS | Ada | `name` | — |
| `username` | CSV & JSON | **wajib** | **Tidak** | — | derivasi deterministik dari `name` (slug + suffix indeks) |
| `agent_id` / `user_id` | semua | **wajib**, = indeks 0..N−1 | **Tidak** | — | indeks urut persona dalam run (deterministik) |
| `age` | JSON Reddit | **wajib** (KeyError) | **Tidak** | — | default statis / LLM tambahan / derivasi |
| `gender` | JSON Reddit | **wajib** | **Tidak** | — | default statis (`"other"`) / LLM tambahan |
| `mbti` | JSON Reddit | **wajib** | **Tidak** | — | LLM tambahan / derivasi dari `personality` / default statis |
| `country` | JSON Reddit | **wajib** | **Tidak** | — | default statis (mis. sesuai universe saham) |
| `activity_level` | `agent_configs` | dibaca runner | **Tidak** | — | derivasi dari `philosophy_label`/`communication_style` / default statis 0.5 |
| `active_hours` | `agent_configs` | dibaca runner | **Tidak** | — | default statis (mis. `list(range(0,24))`) — penting untuk 10 round |
| `agent_id`, `entity_name` | `agent_configs` | dibaca runner | sebagian | `name` → `entity_name` | `agent_id` = indeks |
| `response_delay_min/max` | `agent_configs` | **tidak dibaca** | Tidak | — | tidak perlu (dead config) kecuali runner diubah |
| `sentiment_bias` | `agent_configs` | **tidak dibaca** | Tidak | — | tidak perlu; **catatan:** sentimen/stance investasi sengaja Fase 11b, bukan dari identitas |
| `influence_weight` | `agent_configs` | hanya pilih poster awal | Tidak | — | default statis / hanya jika `initial_posts` dipilih via generator |
| `posts_per_hour`, `comments_per_hour`, `stance` | `agent_configs` | **tidak dibaca** | Tidak | — | tidak perlu |
| `entity_uuid`, `entity_type` | `agent_configs` | tidak dibaca | Tidak | — | placeholder |
| `initial_posts[]` | `event_config` | opsional | Tidak | — | perlu dirakit dari sumber lain (mis. berita Fase 8) — di luar persona |
| Follow edges (complete graph) | DB `follow` + `AgentGraph` | ingin diputuskan | Tidak | — | dibangun di round 0 via `ManualAction(FOLLOW)`; deterministik |
| Field yang ada di Fase 10 tapi **tak punya slot** OASIS | — | — | `biases`, `philosophy_label`, `edge_vs_others`, `tagline` | — | harus dilebur ke teks `persona`/`user_char` atau hilang |

---

## 9. Temuan tambahan yang relevan untuk desain

1. **`oasis_profile_generator.py` sudah bukan generator OASIS murni.** Fase 3 menambah
   `generate_investor_persona` / `generate_sharia_persona` dan `OasisAgentProfile` masih ada, tetapi fungsi
   penulis file (`_save_twitter_csv`, `_save_reddit_json`) masih utuh dan bisa dipakai ulang sebagai
   referensi/bahkan sebagai kontrak format. Yang tak lagi terhubung ke Zep/entity adalah jalur Fase 10.
2. **Default `country` `"中国"` dan default `age=30`, `mbti="ISTJ"`** di `_save_reddit_json` — kalau adapter
   memakai jalur ini tanpa mengisi, hasilnya akan berkonteks Tiongkok.
3. **`agent.py` menulis log ke `./log/`** pada saat import (`logging.FileHandler`), bergantung `cwd`.
   Runner mengatur `cwd=sim_dir`; kalau `./log` tidak ada di `cwd`, import dapat gagal. (Skrip
   `init_logging_for_simulation` tampaknya menanganinya — tidak diverifikasi eksekusi penuh.)
4. **Jumlah agent per round**: `agents_per_hour_min/max` default 5/20 — dengan 8 persona, `random.sample(candidates, min(target, len(candidates)))`
   aman, tetapi kandidat masih disaring `activity_level` (probabilistik) dan `active_hours`.
5. **`SimulationRunner` menunggu command setelah selesai** (wait-for-commands mode); selesai natural
   dideteksi dari event `simulation_end`, bukan process exit (`--no-wait` tersedia).
6. **Prasyarat pemanggil**: `SimulationRunner.start_simulation` mengharuskan `simulation_config.json` ada
   di `uploads/simulations/<id>/`, serta `SimulationManager` (yang meng-*prepare*) masih memakai `graph_id` + Zep.
   Adapter 11a harus memutuskan apakah menulis 3 artefak itu langsung (melewati `SimulationManager.prepare_simulation`) atau
   lewat manager.

---

## 10. Kesimpulan eksplisit

**`simulation_runner.py` + skrip OASIS = fungsional dan pernah lengkap end-to-end, tetapi TIDAK "siap dengan sedikit adapter"
dalam arti sempit.** Penilaian jujur per komponen:

**Sudah lengkap & terbukti jalan (bisa dipakai apa adanya):**
- Launcher/monitor (`simulation_runner.py`), IPC, action logging, loop round, dual-platform paralel.
- Dependensi (`camel-oasis`, `camel-ai`) tercantum dan terpasang; `import oasis` sukses.
- Bukti run sukses 10/10 round di 2 platform (`sim_11bce4ea88a8`).

**Adapter tipis yang cukup (persona → file):**
- Teks persona (`user_char` / `persona`), `bio`, `username`, indeks `agent_id` — bisa diturunkan dari
  field Fase 10 + deterministik.
- 4 field demografis Reddit — bisa default statis atau derivasi; **wajib ada** (bukan opsional).
- `agent_configs`: hanya **4 field** yang benar-benar dibaca (`agent_id`, `entity_name`, `activity_level`, `active_hours`).

**Butuh dibangun (bukan sekadar adapter, tidak ada di kode existing):**
1. **Follow network complete graph** — tidak ada contoh/default; `generate_*_agent_graph` tidak membuat edge;
   run nyata punya tabel `follow` kosong. Harus ditambahkan (mis. `ManualAction(FOLLOW)` di round 0) —
   ini **perubahan skrip runner**, bukan sekadar data.
2. **Pembentukan `simulation_config.json` tanpa Zep** — jalur existing (`SimulationManager` + `ZepEntityReader` +
   `SimulationConfigGenerator` yang mengandalkan entitas Zep) tidak cocok dengan 8 persona Fase 10.
   Perlu ditulis langsung atau lewat jalur baru.
3. **`initial_posts`** — tanpa ini round 0 kosong dan simulasi hanya berjalan dari aktivasi LLM acak; sumber
   kontennya di luar skema persona.

**Bagian yang tidak pernah selesai / tidak fungsional sejak awal (dilaporkan apa adanya):**
- `response_delay_*`, `sentiment_bias`, `stance`, `posts_per_hour`, `comments_per_hour`, dan seluruh `PlatformConfig`
  (`recency_weight`, `popularity_weight`, `relevance_weight`, `viral_threshold`, `echo_chamber_strength`) **di-generate
  oleh LLM tetapi tidak pernah dikonsumsi kode manapun di repo ini**. Jika 11a berencana memakainya
  ("activity profile: response latency, engagement frequency, sentiment baseline, influence weight"), efeknya
  **nol pada simulasi** kecuali runner dimodifikasi untuk membacanya. Tidak dapat dipastikan apakah upstream MiroFish
  memang sengaja demikian; yang pasti: di sini tidak ada.
- `user_name` NULL di DB dan `user_id`/`name` di file profil diabaikan OASIS (perilaku upstream, tidak merusak).
- `scripts/test_profile_format.py` menyimpang dari kontrak nyata (stale) dan bukan test otomatis.
- **Tidak ada test otomatis** untuk jalur input OASIS sama sekali.

**Risiko yang belum diverifikasi (tidak dieksekusi dalam investigasi ini):**
- Apakah jalur Twitter (`twhin-bert`) masih bisa mengunduh model HF di environment sekarang.
- Apakah `simulation_runner.py` masih lolos import/berjalan penuh setelah perubahan Zep (test barrier meng-monkeypatch `Popen`).
- Perilaku jika persona ≤ 8 agent dengan `active_hours` default dan hanya 10 round.

**Ringkasan satu kalimat:** mesin OASIS-nya lengkap dan pernah jalan, tetapi input persona-nya adalah
**tiga artefak** (2 file profil + config) dengan hanya sebagian kecil field yang bermakna; bagian
terbesar yang belum ada adalah **follow-network complete graph** dan **jalur pembuatan config tanpa Zep** —
keduanya perlu dibangun baru, bukan sekadar mapping field.
