import os
import shutil
import socket

import toml
from loguru import logger

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
config_file = f"{root_dir}/config.toml"
_CONTAINER_CGROUP_MARKERS = ("docker", "containerd", "kubepods", "libpod", "podman")
_DOCKER_HOST_GATEWAY_NAME = "host.docker.internal"


def is_running_in_container(
    dockerenv_path: str = "/.dockerenv",
    containerenv_path: str = "/run/.containerenv",
    cgroup_path: str = "/proc/1/cgroup",
) -> bool:
    """
    判断当前进程是否运行在容器内。

    这个判断主要用于 Ollama 默认地址选择：
    - 普通本机运行时，`localhost` 指向用户机器本身；
    - Docker 容器内，`localhost` 指向容器自己，访问宿主机 Ollama
      通常需要使用 `host.docker.internal`。

    不能只判断 `/proc/1/cgroup` 是否存在，因为普通 Linux 也会有这个文件。
    这里只在检测到明确的容器标记时返回 True，避免误伤非 Docker Linux 用户。
    参数保留为可注入路径，便于单元测试覆盖不同运行环境。
    """
    if os.path.isfile(dockerenv_path) or os.path.isfile(containerenv_path):
        return True

    try:
        with open(cgroup_path, mode="r", encoding="utf-8") as fp:
            cgroup_content = fp.read().lower()
    except OSError:
        return False

    return any(marker in cgroup_content for marker in _CONTAINER_CGROUP_MARKERS)


def _can_resolve_hostname(hostname: str) -> bool:
    try:
        socket.gethostbyname(hostname)
    except OSError:
        return False
    return True


def _decode_linux_route_gateway(hex_gateway: str) -> str:
    # /proc/net/route 里的 Gateway 是 16 进制小端序，例如 010011AC 表示
    # 172.17.0.1。这里单独解析，是为了在原生 Linux Docker 没有
    # host.docker.internal DNS 记录时，还能尝试访问容器默认网关上的宿主机。
    if len(hex_gateway) != 8:
        raise ValueError("invalid gateway length")

    octets = [
        str(int(hex_gateway[index : index + 2], 16))
        for index in range(6, -1, -2)
    ]
    return ".".join(octets)


def get_container_default_gateway_ip(route_path: str = "/proc/net/route") -> str:
    """
    读取 Linux 容器里的默认网关 IP。

    Docker Desktop 通常提供 `host.docker.internal`，但原生 Linux Docker
    默认不一定提供这个 DNS 名称。默认网关通常可以作为访问宿主机服务的
    兜底地址；如果用户的 Ollama 只监听 127.0.0.1，则仍需要用户让
    Ollama 监听宿主机网卡或手动配置 `ollama_base_url`。
    """
    try:
        with open(route_path, mode="r", encoding="utf-8") as fp:
            route_lines = fp.readlines()
    except OSError:
        return ""

    for line in route_lines[1:]:
        fields = line.strip().split()
        if len(fields) < 3:
            continue

        destination = fields[1]
        gateway = fields[2]
        if destination != "00000000" or gateway == "00000000":
            continue

        try:
            return _decode_linux_route_gateway(gateway)
        except ValueError:
            logger.warning(f"invalid container gateway route entry: {line.strip()}")
            return ""

    return ""


def get_default_ollama_base_url() -> str:
    """
    返回 Ollama 的默认 OpenAI-compatible base_url。

    用户显式配置 `ollama_base_url` 时不会走这里；这里只处理“未配置时的
    最佳默认值”。容器内默认指向宿主机，普通本机运行默认指向 localhost。
    """
    if not is_running_in_container():
        return "http://localhost:11434/v1"

    if _can_resolve_hostname(_DOCKER_HOST_GATEWAY_NAME):
        return f"http://{_DOCKER_HOST_GATEWAY_NAME}:11434/v1"

    gateway_ip = get_container_default_gateway_ip()
    if gateway_ip:
        logger.info(
            "host.docker.internal is not resolvable, fallback to container "
            f"default gateway for Ollama: {gateway_ip}"
        )
        return f"http://{gateway_ip}:11434/v1"

    logger.warning(
        "failed to resolve host.docker.internal and container default gateway; "
        "fallback to host.docker.internal for Ollama"
    )
    return f"http://{_DOCKER_HOST_GATEWAY_NAME}:11434/v1"


def load_config():
    # fix: IsADirectoryError: [Errno 21] Is a directory: '/MoneyPrinterTurbo/config.toml'
    if os.path.isdir(config_file):
        shutil.rmtree(config_file)

    if not os.path.isfile(config_file):
        example_file = f"{root_dir}/config.example.toml"
        if os.path.isfile(example_file):
            shutil.copyfile(example_file, config_file)
            logger.info("copy config.example.toml to config.toml")

    logger.info(f"load config from file: {config_file}")

    try:
        _config_ = toml.load(config_file)
    except Exception as e:
        logger.warning(f"load config failed: {str(e)}, try to load as utf-8-sig")
        with open(config_file, mode="r", encoding="utf-8-sig") as fp:
            _cfg_content = fp.read()
            _config_ = toml.loads(_cfg_content)
    return _config_


def save_config():
    with open(config_file, "w", encoding="utf-8") as f:
        _cfg["app"] = app
        _cfg["azure"] = azure
        _cfg["siliconflow"] = siliconflow
        _cfg["ui"] = ui
        f.write(toml.dumps(_cfg))


_cfg = load_config()
app = _cfg.get("app", {})
whisper = _cfg.get("whisper", {})
proxy = _cfg.get("proxy", {})
azure = _cfg.get("azure", {})
siliconflow = _cfg.get("siliconflow", {})
ui = _cfg.get(
    "ui",
    {
        "hide_log": False,
    },
)

hostname = socket.gethostname()

log_level = _cfg.get("log_level", "DEBUG")
listen_host = _cfg.get("listen_host", "0.0.0.0")
listen_port = _cfg.get("listen_port", 8080)
project_name = _cfg.get("project_name", "MoneyPrinterTurbo")
project_description = _cfg.get(
    "project_description",
    "<a href='https://github.com/harry0703/MoneyPrinterTurbo'>https://github.com/harry0703/MoneyPrinterTurbo</a>"
    "<br><small>Supported by <a href='https://aihubmix.com/?aff=CEve'>AIHubMix</a></small>",
)
project_version = _cfg.get("project_version", "1.3.0")
reload_debug = False

app["redis_host"] = os.getenv(
    "MPT_APP_REDIS_HOST",
    os.getenv("REDIS_HOST", app.get("redis_host", "localhost")),
)

imagemagick_path = app.get("imagemagick_path", "")
if imagemagick_path and os.path.isfile(imagemagick_path):
    os.environ["IMAGEMAGICK_BINARY"] = imagemagick_path

ffmpeg_path = app.get("ffmpeg_path", "")
if ffmpeg_path and os.path.isfile(ffmpeg_path):
    os.environ["IMAGEIO_FFMPEG_EXE"] = ffmpeg_path


# ── 唤星 reel 收编：daemon 运行时注入（env 优先于 config.toml）─────────────────────────
# 设计 doc19 §6：模型/素材 key 唯一权威是 PDC（daemon 注入 env），sidecar 不持默认值兜底。
# 改动集中在此一处（config.py）+ 各 service 薄改读这些键，便于 upstream rebase（§6.5）。
# 契约见 doc19 §4.3 配置注入表 与 实施清单 P1。
def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def apply_reel_runtime_env() -> None:
    """把 daemon 注入的 REEL_* env 叠加到已加载的 config（env 覆盖 config.toml）。

    env 未设时回落 config.toml（本地 dev 直跑不受影响）。
    """
    # 1) LLM 网关收编（doc19 §6.1）：锁 llm_provider=openai 兼容、base_url/key 指向 new-api。
    gateway_base = os.environ.get("REEL_GATEWAY_BASE_URL", "").strip()
    gateway_key = os.environ.get("REEL_GATEWAY_API_KEY", "").strip()
    if gateway_base and gateway_key:
        app["llm_provider"] = "openai"
        app["openai_base_url"] = gateway_base
        app["openai_api_key"] = gateway_key
        # 模型名可选 override；未设则沿用 config.toml 的 openai_model_name。
        model_llm = os.environ.get("REEL_MODEL_LLM", "").strip()
        if model_llm:
            app["openai_model_name"] = model_llm

    # 主模型上游连挂时按序切换的兜底候选（doc19 §6.1 failover；env 未设 = 空列表，行为同前）。
    fallbacks = _csv_env("REEL_MODEL_LLM_FALLBACKS")
    if fallbacks:
        app["llm_model_fallbacks"] = fallbacks

    # 2) TTS（doc19 §6.2）：默认 edge 免费；provider/voice 由 daemon 注入。
    tts_provider = os.environ.get("REEL_TTS_PROVIDER", "").strip().lower()
    if tts_provider:
        app["reel_tts_provider"] = tts_provider
    voice_name = os.environ.get("REEL_VOICE_NAME", "").strip()
    if voice_name:
        app["reel_voice_name"] = voice_name
    voice_rate = os.environ.get("REEL_VOICE_RATE", "").strip()
    if voice_rate:
        app["reel_voice_rate"] = voice_rate

    # 3) 字幕（doc19 §6.4）：默认 edge（不下 whisper）。
    subtitle_provider = os.environ.get("REEL_SUBTITLE_PROVIDER", "").strip().lower()
    if subtitle_provider:
        app["subtitle_provider"] = subtitle_provider
    whisper_size = os.environ.get("REEL_WHISPER_MODEL_SIZE", "").strip()
    if whisper_size:
        whisper["model_size"] = whisper_size

    # 4) 库存素材 key 收编（doc19 §6.3 M2）：平台兜底 + owner 自填，多 key 轮换。
    pexels_keys = _csv_env("REEL_PEXELS_API_KEYS")
    if pexels_keys:
        app["pexels_api_keys"] = pexels_keys
    pixabay_keys = _csv_env("REEL_PIXABAY_API_KEYS")
    if pixabay_keys:
        app["pixabay_api_keys"] = pixabay_keys

    # 5) 存储根（doc19 §5）：产物/素材缓存全部落 REEL_MEDIA_ROOT 子目录（见 utils.storage_dir）。
    #    实际重定向在 app/utils/utils.py::storage_dir() 读 REEL_MEDIA_ROOT，这里仅记录用于诊断。

    # 6) 砍第三方发布（doc19 §9 M4）：硬编码关闭，daemon 不应注入开启。
    app["upload_post_enabled"] = False
    app["upload_post_auto_upload"] = False


def gateway_credentials():
    """唤星 reel 收编：读网关 env (base_url, api_key)；任一缺失返回 None（回落原生 provider）。

    网关模式下 LLM 客户端强制走 new-api OpenAI 兼容端点（PDC 注入的模型名可能不含 'gpt'，
    按模型名分派会误路由）。返回 (base_url, api_key) 或 None。
    """
    base_url = os.environ.get("REEL_GATEWAY_BASE_URL", "").strip()
    api_key = os.environ.get("REEL_GATEWAY_API_KEY", "").strip()
    return (base_url, api_key) if base_url and api_key else None


def reel_llm_model_fallbacks() -> list[str]:
    """读 LLM failover 兜底候选列表（daemon 经 REEL_MODEL_LLM_FALLBACKS 注入）。"""
    value = app.get("llm_model_fallbacks", [])
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in (value or []) if str(item).strip()]


apply_reel_runtime_env()

logger.info(f"{project_name} v{project_version}")
