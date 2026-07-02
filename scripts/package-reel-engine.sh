#!/usr/bin/env bash
set -euo pipefail

# 打包并发布 MoneyPrinterTurbo（reel）短视频合成引擎为 downloadable_local 分发包（模块 14 / 实施 11，doc19）。
#
# 与 package-film-engine.sh 同构、同发布管线，仅 app_id=reel + 命名前缀换 reel（doc19 P2-pkg：
# 「复用 film 的包发布脚本（参数化 app_id）」）。设计目标：**一个脚本、不带参数即默认构建并发布
# 当前机器能产出的所有平台架构**，所有上传参数放配置文件 config.yaml（默认=生产）/ config-dev.yaml（本地）。
# 日常发布只需（脚本已随引擎收编进 huanxing-apps/reel-engine/scripts/，紧邻引擎源码）：
#   scripts/package-reel-engine.sh            # 默认读同目录 config.yaml（生产）
#   scripts/package-reel-engine.sh --dev      # 读同目录 config-dev.yaml（本地/测试环境）
#
# 产出 daemon `domains/reel/{prepare,install,locator}.rs` 期望的契约（**与 film 完全一致**，已逐文件镜像）：
#   - 包格式 = **zip**（daemon `install.rs::unpack_zip` 只依赖 zip，不引入 tar/zstd；解压保留 unix 可执行权限）。
#     ⚠ 注意：doc19 §10/正文里历史性写过 `reel-<ver>-<os-arch>.tar.zst`，但**已落地的 daemon 实现是 zip**
#     （install.rs 头注：「包格式 = zip」），故本脚本产 zip 以对齐**实际可运行的 daemon 契约**（非文案）。
#   - 顶层含 `backend/`（fork FastAPI 源码 + `.venv/`），解压后落到 `<install_root>/current/backend`；
#     结构闸门：包内必须有 `backend/api/app.py`（install.rs / locator.rs 硬校验；sidecar 以 `api.app:app`
#     模块式 uvicorn 启动，cwd=backend）。
#   - manifest.json：`{"version","packages":{"<os-arch>":{"key","url","sha256","size"}}}`，对齐
#     `prepare.rs::ReelManifest`（version + packages{os-arch:{url,sha256,size,key}}）；
#     `<os-arch>` 形如 `darwin-aarch64` / `darwin-x86_64` / `linux-x86_64` / `win-x86_64`。
#   - reel 特有：catalog `config_json.engine.bundled_deps=["ffmpeg","imagemagick"]`（本地合成依赖）。本脚本
#     **不**把 ffmpeg/ImageMagick 二进制塞进 zip（跨平台/跨架构二进制随包是平台相关大工程，标 TODO，见下
#     `bundle_deps` 占位函数）；运行期 daemon 预备链负责探测/下载，瘦引擎页据引擎就绪诚实展示依赖态。
#
# 用法:
#   scripts/package-reel-engine.sh [选项] [aarch64|x86_64]
#   - 不带架构参数：构建+发布**当前 OS 默认全架构**（mac=aarch64+x86_64；linux/win=本机架构），
#     或配置 targets 指定的列表。
#   - 带一个架构参数：只构建+发布该架构（兼容旧用法 / 单架构调试）。
#
# 配置（一律可放配置文件，键名见下；优先级 命令行 > 环境变量 > 配置文件 > 内置默认）:
#   --dev             读同目录 config-dev.yaml（本地/测试环境）而非默认的 config.yaml（生产）。
#                     等价于 --env=dev；不给则默认 config.yaml。
#   --env=<name>      指定环境：dev → config-dev.yaml；prod/空 → config.yaml。
#   --config=<file>   显式指定配置文件路径（覆盖 --dev/--env 的默认选择）。亦可用环境变量
#                     REEL_ENGINE_CONFIG 指定。**YAML 扁平 key: value、# 注释，安全逐行解析（不 source、
#                     不引 YAML 库）**。真实上传参数放 config.yaml/config-dev.yaml（均已 .gitignore），
#                     模板见 config.example.yaml。
#   --targets=<list>  目标架构列表（逗号或空格分隔，如 "aarch64,x86_64"）。配置键 targets。
#                     留空 = 当前 OS 默认全架构。跨 OS（linux/win）须在对应 OS 机器/容器上跑本脚本。
#   --version=<v>     包版本（配置键 version；留空则读源 pyproject.toml 的 version）。
#   --src=<dir>       reel fork 源码根目录（配置键 src；默认引擎仓根 huanxing-apps/reel-engine——
#                     含 `api/app.py` 再导出入口 + MPT 原生 `app/` 包；staging 后落为包内
#                     `backend/api/app.py` + `backend/app/`）。
#   --out=<dir>       产物输出目录（配置键 out；默认 <引擎仓根>/.engine-build/reel-engine）。
#   --publish=<url>   云端 API 基址（配置键 REEL_ENGINE_PUBLISH_URL）。给了即开启发布：打包后自动
#                     POST 引擎包到 <url>/api/v1/hasn/app-catalogs/<pk>/engine-package，落公共桶 +
#                     写 config_json.engine + push platform_config（在线 daemon 秒级重拉、自动装引擎）。
#                     服务端**权威**算 sha256/size、与本地交叉校验。需 app-pk + 管理端 token。
#   --app-pk=<id>     发布必填：云端应用目录行 ID（配置键 app_pk；reel 那行主键）。
#   --admin-token=<t> 发布必填：管理端 JWT（配置键 admin_token；放配置文件或环境变量 HASN_ADMIN_TOKEN）。
#   --base-url=<u>    仅手工上传场景：manifest.url=<base>/<包名>（配置键 base_url）。
#                     给 --publish 时无需本项（URL 由云端公共桶分配）。
#   --no-venv         **仅验证打包流水线**：跳过 venv 构建，产出 STRUCTURE-only 包（不可运行）。
#   --help
#
# 可移植性方案（同 film，已落地）:
#   - 自带 standalone Python：把 uv 托管的 python-build-standalone 解释器整树用 `rsync -aL`（解引用所有
#     symlink）拷进 `backend/.venv`，删 PEP 668 `EXTERNALLY-MANAGED` 标记，再用包内 python 把依赖装进
#     其自身 site-packages。产物**无 symlink**，拷到任意机器/路径直接可跑，满足 daemon locator 的
#     `backend/.venv/bin/python` 契约——零 daemon 改动。
#   - 跨架构边界：`uv` 只能装「当前 OS」的 standalone Python；同 OS 跨架构（mac arm64↔x86_64）可经
#     uv/Rosetta，跨 OS（linux / win）须在对应 OS 的机器 / 容器上分别跑本脚本，再各自上传、合并 manifest。

# ---- 参数解析 -------------------------------------------------------------
ARCH_INPUT=""
TARGETS_INPUT="${REEL_ENGINE_TARGETS:-}"
VERSION="${REEL_ENGINE_VERSION:-}"
SRC="${REEL_BACKEND_SRC:-}"
OUT_DIR="${REEL_ENGINE_OUT:-}"
BASE_URL="${REEL_ENGINE_BASE_URL:-}"
PUBLISH_URL="${REEL_ENGINE_PUBLISH_URL:-}"
APP_PK="${REEL_ENGINE_APP_PK:-}"
ADMIN_TOKEN="${HASN_ADMIN_TOKEN:-}"
NO_VENV=0
ENV_NAME=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${REEL_ENGINE_CONFIG:-}"

# usage 打印脚本头注释（行 3 至「参数解析」分隔线），对 header 增长鲁棒（不硬编码行号）。
usage() { sed -n '3,/^# ----/p' "${BASH_SOURCE[0]}" | sed '/^# ----/d; s/^# \{0,1\}//'; }

# 安全解析 YAML 扁平配置文件（key: value，# 注释）——**逐行解析、不 source、不引 YAML 库**（防任意代码执行）。
# 仅取顶层 `key: value`（value 按首个冒号切分，故 URL 里的 :// 不受影响）；缩进/嵌套/列表不支持（本配置全扁平）。
# 仅填充「当前仍为空」的变量，故优先级 = 命令行 > 环境变量 > 配置文件 > 内置默认。
load_config_file() {
  local file="$1" line key value
  [[ -f "${file}" ]] || return 0
  echo "[reel-pkg] 读配置文件: ${file}"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"                       # 去尾部 CR（CRLF 文件兼容）
    line="${line#"${line%%[![:space:]]*}"}"    # 去前导空白
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    [[ "${line}" != *:* ]] && continue          # 非 key: value 行跳过（含 YAML 文档分隔/纯注释）
    key="${line%%:*}"; value="${line#*:}"       # 首个冒号切分（value 里的 :// 保留）
    key="${key//[[:space:]]/}"
    value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"  # 去 value 首尾空白
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"      # 去成对引号
    case "${key}" in
      targets) [[ -z "${TARGETS_INPUT}" ]] && TARGETS_INPUT="${value}" ;;
      publish_url) [[ -z "${PUBLISH_URL}" ]] && PUBLISH_URL="${value}" ;;
      app_pk) [[ -z "${APP_PK}" ]] && APP_PK="${value}" ;;
      admin_token) [[ -z "${ADMIN_TOKEN}" ]] && ADMIN_TOKEN="${value}" ;;
      version) [[ -z "${VERSION}" ]] && VERSION="${value}" ;;
      src) [[ -z "${SRC}" ]] && SRC="${value}" ;;
      out) [[ -z "${OUT_DIR}" ]] && OUT_DIR="${value}" ;;
      base_url) [[ -z "${BASE_URL}" ]] && BASE_URL="${value}" ;;
      *) echo "[reel-pkg] ⚠ 配置文件未知键，忽略: ${key}" >&2 ;;
    esac
  done < "${file}"
  return 0   # 收口：末行 case 的可选赋值 `[[ -z VAR ]] && VAR=val` 在 VAR 已由环境变量预置时短路返回 1，
             # 该 1 会成为本函数返回值 → set -e 在调用处误判失败而静默早退。显式 return 0 解耦。
}

# 先扫一遍找 --config / --dev / --env（让命令行能指定配置文件/环境），再加载配置（不覆盖已设环境变量）。
for arg in "$@"; do
  case "${arg}" in
    --config=*) CONFIG_FILE="${arg#--config=}" ;;
    --dev) ENV_NAME="dev" ;;
    --env=*) ENV_NAME="${arg#--env=}" ;;
  esac
done
# 默认 config.yaml（生产）；--dev / --env=dev → config-dev.yaml（本地）；--config 显式路径优先级最高。
if [[ -z "${CONFIG_FILE}" ]]; then
  case "${ENV_NAME}" in
    dev | test | local) CONFIG_FILE="${SCRIPT_DIR}/config-dev.yaml" ;;
    *) CONFIG_FILE="${SCRIPT_DIR}/config.yaml" ;;
  esac
fi
load_config_file "${CONFIG_FILE}"

for arg in "$@"; do
  case "${arg}" in
    --config=* | --dev | --env=*) ;; # 已在上面处理
    --targets=*) TARGETS_INPUT="${arg#--targets=}" ;;
    --version=*) VERSION="${arg#--version=}" ;;
    --src=*) SRC="${arg#--src=}" ;;
    --out=*) OUT_DIR="${arg#--out=}" ;;
    --base-url=*) BASE_URL="${arg#--base-url=}" ;;
    --publish=*) PUBLISH_URL="${arg#--publish=}" ;;
    --app-pk=*) APP_PK="${arg#--app-pk=}" ;;
    --admin-token=*) ADMIN_TOKEN="${arg#--admin-token=}" ;;
    --no-venv) NO_VENV=1 ;;
    --help | -h) usage; exit 0 ;;
    aarch64 | arm64) ARCH_INPUT=aarch64 ;;
    x86_64 | amd64) ARCH_INPUT=x86_64 ;;
    *)
      echo "未知参数: ${arg}" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# 目标 OS 由构建主机决定（跨 OS 的 Python 构建不在 bash 单机范围内；同 OS 跨架构可经 uv/Rosetta）。
case "$(uname -s)" in
  Darwin) OS_KEY=darwin; PY_OS=macos ;;
  Linux) OS_KEY=linux; PY_OS=linux ;;
  MINGW* | MSYS* | CYGWIN*) OS_KEY=win; PY_OS=windows ;;
  *) echo "不支持的构建主机 OS: $(uname -s)" >&2; exit 1 ;;
esac

# 当前 OS 默认能构建的架构集合（不带参数、且配置未指定 targets 时用）。
default_targets_for_os() {
  case "${OS_KEY}" in
    darwin) echo "aarch64 x86_64" ;; # mac 可经 uv/Rosetta 跨架构
    linux)
      case "$(uname -m)" in
        aarch64 | arm64) echo "aarch64" ;;
        *) echo "x86_64" ;;
      esac ;;
    win) echo "x86_64" ;;
  esac
}

# 解析最终目标架构列表：命令行单架构 > --targets/配置 > 当前 OS 默认全架构。
if [[ -n "${ARCH_INPUT}" ]]; then
  TARGETS="${ARCH_INPUT}"
elif [[ -n "${TARGETS_INPUT}" ]]; then
  TARGETS="${TARGETS_INPUT//,/ }" # 逗号亦可
else
  TARGETS="$(default_targets_for_os)"
fi
# 校验架构合法（只认 aarch64/x86_64）。
for t in ${TARGETS}; do
  case "${t}" in
    aarch64 | x86_64) ;;
    *) echo "[reel-pkg] 未知目标架构: ${t}（仅支持 aarch64 / x86_64）" >&2; exit 1 ;;
  esac
done

# ---- 全局前置校验（影响所有架构的，一次性 fail-fast）-----------------------
ENGINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 本脚本随引擎收编进 huanxing-apps/reel-engine/scripts/，故 ENGINE_ROOT=引擎仓根（scripts/ 的上一层）。
# 该 fork（branch huanxing-reel，option-3-clean）保留 MPT 原生 `app/` 包于**根**，并在**根**新增
# `api/app.py`（仅 `from app.asgi import app` 再导出已加固 app）。故 SRC=引擎仓根（含 `app/`+`api/app.py`），
# staging `rsync ${SRC}/ → ${STAGE}/backend/` 后得 `backend/app/`+`backend/api/app.py`——正是 daemon
# locator/install 期望的包内布局（`api.app:app` 模块式 uvicorn 启动，cwd=backend → `import app` 原生可解析）。
SRC="${SRC:-${ENGINE_ROOT}}"
OUT_DIR="${OUT_DIR:-${ENGINE_ROOT}/.engine-build/reel-engine}"

if [[ ! -f "${SRC}/api/app.py" ]]; then
  echo "[reel-pkg] 找不到 reel fork 源（缺 ${SRC}/api/app.py）；用 --src 指定 fork 根。" >&2
  echo "[reel-pkg] 提示：daemon locator/install 要求包内入口 backend/api/app.py（sidecar 以 api.app:app 模块式启动）；" >&2
  echo "[reel-pkg]      fork(huanxing-reel) 根应有 api/app.py（再导出 app.asgi:app）+ MPT 原生 app/ 包。" >&2
  exit 1
fi
if [[ ! -d "${SRC}/app" ]]; then
  echo "[reel-pkg] fork 源缺 MPT 原生 app/ 包（${SRC}/app）；SRC 应指 fork 根而非其子目录。" >&2
  exit 1
fi

# 版本：未显式给则从 pyproject.toml 读首个 version 行。多架构必须同版本（manifest/云端约束）。
if [[ -z "${VERSION}" && -f "${SRC}/pyproject.toml" ]]; then
  VERSION="$(awk -F'"' '/^version[[:space:]]*=/{print $2; exit}' "${SRC}/pyproject.toml")"
fi
if [[ -z "${VERSION}" ]]; then
  echo "[reel-pkg] 无法确定版本（pyproject.toml 无 version，且未给 --version / REEL_ENGINE_VERSION）" >&2
  exit 1
fi

if [[ "${NO_VENV}" != "1" ]]; then
  command -v uv >/dev/null 2>&1 || { echo "[reel-pkg] 需要 uv 安装 standalone Python（或加 --no-venv 仅验证流水线）" >&2; exit 1; }
  # Windows 用 cp -R 拷贝解释器树（standalone 标准库树无 symlink）；其余平台用 rsync -aL 解引用 symlink。
  if [[ "${OS_KEY}" != "win" ]]; then
    command -v rsync >/dev/null 2>&1 || { echo "[reel-pkg] 需要 rsync 实体化拷贝解释器树（-aL 解引用 symlink）" >&2; exit 1; }
  fi
fi

# 发布前置：fail-fast，缺要素立即报错（别等打完几百 MB 才发现没法上传）。
if [[ -n "${PUBLISH_URL}" ]]; then
  [[ -n "${APP_PK}" ]] || { echo "[reel-pkg] 发布需要 --app-pk / REEL_ENGINE_APP_PK（云端应用目录行ID，reel 那行主键）" >&2; exit 1; }
  [[ -n "${ADMIN_TOKEN}" ]] || { echo "[reel-pkg] 发布需要 --admin-token / HASN_ADMIN_TOKEN（管理端 JWT）" >&2; exit 1; }
  command -v curl >/dev/null 2>&1 || { echo "[reel-pkg] 发布需要 curl 上传引擎包" >&2; exit 1; }
fi

echo "[reel-pkg] OS=${OS_KEY} 目标架构=[${TARGETS}] 版本=${VERSION} 源=${SRC}"
[[ "${NO_VENV}" == "1" ]] && echo "[reel-pkg] ⚠ --no-venv：仅验证流水线，产出 STRUCTURE-only 包（不可运行）"
[[ -n "${PUBLISH_URL}" ]] && echo "[reel-pkg] 发布开启 → ${PUBLISH_URL}（app-pk=${APP_PK}）" || echo "[reel-pkg] 未配 --publish/REEL_ENGINE_PUBLISH_URL：只打包不发布"

# reel 特有：本地合成依赖（ffmpeg/ImageMagick）随包预备占位。
# 现状：**不**把二进制塞进 zip——跨平台/跨架构 ffmpeg/ImageMagick 静态二进制各自来源、体积大、许可各异，
# 是平台相关大工程（[[project_macos_desktop_dual_arch_packaging]] 双架构坑）。catalog 标
# bundled_deps=["ffmpeg","imagemagick"]，运行期 daemon 预备链探测/下载、设 IMAGEIO_FFMPEG_EXE；
# 瘦引擎页据「引擎就绪」诚实展示依赖态，不伪造打勾。待平台二进制就绪后在此函数内把二进制拷进
# backend/bundled_deps/<os-arch>/ 并由 daemon 预备链消费。
bundle_deps_placeholder() {
  local stage_backend="$1" os_arch="$2"
  # TODO（平台相关）：拷贝 ffmpeg/ImageMagick 静态二进制 → ${stage_backend}/bundled_deps/${os_arch}/。
  # 现仅写一个清单占位，标明随包依赖意图（daemon 预备链可读取，未就绪时回落探测系统/按需下载）。
  mkdir -p "${stage_backend}/bundled_deps"
  cat > "${stage_backend}/bundled_deps/MANIFEST.txt" <<EOF
# reel bundled_deps 占位（${os_arch}）。
# 本期未随包二进制；catalog bundled_deps=["ffmpeg","imagemagick"] 声明意图，
# 运行期 daemon 预备链探测系统安装 / 按需下载（doc19 §8 步骤 3.5）。
ffmpeg=runtime-prepared
imagemagick=runtime-prepared
EOF
}

# ---- 单架构处理：staging → venv → zip → manifest →（可选）发布 -------------
# 用 exit 1 在任一架构失败时整体中止（fail-fast）；已发布的架构云端已累积，重跑会补齐其余。
process_one_arch() {
  local ARCH_KEY="$1"
  local OS_ARCH="${OS_KEY}-${ARCH_KEY}"
  echo
  echo "========== [${OS_ARCH}] =========="

  # staging：复制 backend 源（剔除开发垃圾），venv 落 backend/.venv。
  local STAGE="${OUT_DIR}/stage-${OS_ARCH}"
  rm -rf "${STAGE}"
  mkdir -p "${STAGE}/backend"
  # 剔除：源自带 .venv（含 .venv-reel-p0）、缓存、运行期临时产物、VCS、字节码、本机 config.toml
  # （运行期 daemon 注入 env）、已砍的 Streamlit webui（doc19 §4.2）、docs、测试、P0 脚手架。
  # ⚠ 绝不剔 `resource/`（fonts/songs 是合成运行期必需数据，app.utils 经 root_dir() 读 `<backend>/resource`）、
  # `app/`（MPT 原生包）、`api/`（再导出入口）——否则装出来的包缺代码/资源起不来。
  local COPY_EXCLUDES=(
    --exclude='.venv' --exclude='.venv-*' --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache'
    --exclude='.ruff_cache' --exclude='temp/*' --exclude='tests' --exclude='test' --exclude='.git'
    --exclude='node_modules' --exclude='config.toml' --exclude='config.yaml'
    --exclude='webui' --exclude='docs' --exclude='reel_p0_compose.py' --exclude='storage/*'
  )
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "${COPY_EXCLUDES[@]}" "${SRC}/" "${STAGE}/backend/"
  else
    cp -R "${SRC}/." "${STAGE}/backend/"   # /. 拷内容而非把 backend 目录多套一层（Git Bash cp 语义）
    ( cd "${STAGE}/backend" && rm -rf .venv .venv-* .git node_modules tests test .pytest_cache .ruff_cache \
        config.toml config.yaml webui docs reel_p0_compose.py \
        && find . -name '__pycache__' -type d -prune -exec rm -rf {} + \
        && find . -name '*.pyc' -delete \
        && find storage temp -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true )
  fi
  # staging 结构闸门：与 install.rs / locator.rs 同一硬校验，提前在打包侧失败而非等下载后才发现坏包。
  # 入口 + MPT 原生包 + 合成资源三者缺一不可（缺 app/ → import 失败；缺 resource/ → 合成期崩）。
  [[ -f "${STAGE}/backend/api/app.py" ]] || { echo "[reel-pkg] staging 异常：缺 backend/api/app.py" >&2; exit 1; }
  [[ -d "${STAGE}/backend/app" ]]        || { echo "[reel-pkg] staging 异常：缺 backend/app/（MPT 原生包）" >&2; exit 1; }
  [[ -d "${STAGE}/backend/resource" ]]   || { echo "[reel-pkg] staging 异常：缺 backend/resource/（合成期 fonts/songs 必需）" >&2; exit 1; }

  # reel 特有：本地合成依赖随包占位（见 bundle_deps_placeholder 注释）。
  bundle_deps_placeholder "${STAGE}/backend" "${OS_ARCH}"

  # venv：自带 standalone Python（relocatable）。Unix：rsync -aL 实体化 symlink（.venv/bin/python3.12）；
  # Windows：解释器树无 symlink 且 python.exe 在树根，cp -R 整树到 .venv（.venv/python.exe 自定位 stdlib，
  # 无 trampoline、可重定位）——daemon gateway 的 Windows 候选含 .venv/python.exe。详见脚本头注。
  if [[ "${NO_VENV}" != "1" ]]; then
    local PY_SPEC="cpython-3.12-${PY_OS}-${ARCH_KEY}"
    local VENV="${STAGE}/backend/.venv"
    echo "[reel-pkg] 安装目标架构 standalone Python: ${PY_SPEC}"
    uv python install "${PY_SPEC}"
    local PY_DIR_ROOT PY_HOME SRC_PY VENV_PY
    PY_DIR_ROOT="$(uv python dir 2>/dev/null || echo "${HOME}/.local/share/uv/python")"
    PY_HOME="$(ls -d "${PY_DIR_ROOT}"/cpython-3.12.*-"${PY_OS}"-"${ARCH_KEY}"-none 2>/dev/null | sort -V | tail -1)"
    # 解释器与目标 venv 内 python 路径按 OS 分叉：Windows=树根 python.exe（无 bin/）；其余=bin/python3.12。
    if [[ "${OS_KEY}" == "win" ]]; then
      SRC_PY="${PY_HOME}/python.exe"; VENV_PY="${VENV}/python.exe"
    else
      SRC_PY="${PY_HOME}/bin/python3.12"; VENV_PY="${VENV}/bin/python3.12"
    fi
    if [[ -z "${PY_HOME}" || ! -x "${SRC_PY}" ]]; then
      echo "[reel-pkg] 定位 standalone Python 安装根失败（PY_DIR_ROOT=${PY_DIR_ROOT}，期望 cpython-3.12.*-${PY_OS}-${ARCH_KEY}-none 内 python）" >&2
      exit 1
    fi
    rm -rf "${VENV}"; mkdir -p "${VENV}"
    if [[ "${OS_KEY}" == "win" ]]; then
      echo "[reel-pkg] 拷贝解释器树 → ${VENV}（cp -R；Windows 标准库树无 symlink，python.exe 在树根自定位）"
      cp -R "${PY_HOME}/." "${VENV}/"
    else
      echo "[reel-pkg] 实体化拷贝解释器树 → ${VENV}（rsync -aL 解引用 symlink）"
      rsync -aL "${PY_HOME}/" "${VENV}/"
    fi
    echo "[reel-pkg] 移除 PEP 668 EXTERNALLY-MANAGED 标记（引擎私有 python，允许装依赖）"
    find "${VENV}" -name EXTERNALLY-MANAGED -delete
    echo "[reel-pkg] 安装依赖到自身 site-packages (requirements.txt)"
    "${VENV_PY}" -m pip install --no-input --no-warn-script-location -r "${SRC}/requirements.txt"
    # symlink 守卫：daemon unpack_zip 不还原 symlink，包内任何 symlink 都会损坏。
    if find "${STAGE}" -type l | grep -q .; then
      echo "[reel-pkg] ✗ 包内仍存在 symlink（daemon 解压不还原 symlink → 包会损坏）：" >&2
      find "${STAGE}" -type l >&2
      exit 1
    fi
  fi

  # 打包 zip（顶层 backend/）+ sha256 + size。
  mkdir -p "${OUT_DIR}"
  local PKG_NAME="reel-${OS_ARCH}-${VERSION}.zip"
  local PKG_PATH="${OUT_DIR}/${PKG_NAME}"
  rm -f "${PKG_PATH}"
  echo "[reel-pkg] 打包 → ${PKG_PATH}"
  if command -v zip >/dev/null 2>&1; then
    ( cd "${STAGE}" && zip -r -q -X "${PKG_PATH}" backend )
  else
    # 无 zip（如 Windows Git Bash）：用 python zipfile 打包，保持顶层 backend/（deflate 压缩）。
    ( cd "${STAGE}" && PKG_OUT="${PKG_PATH}" python3 - <<'PY'
import os, zipfile
out = os.environ["PKG_OUT"]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _dirs, files in os.walk("backend"):
        for name in files:
            full = os.path.join(root, name)
            z.write(full, full.replace(os.sep, "/"))
PY
    )
  fi

  local SHA256 SIZE
  if command -v sha256sum >/dev/null 2>&1; then
    SHA256="$(sha256sum "${PKG_PATH}" | awk '{print $1}')"
  else
    SHA256="$(shasum -a 256 "${PKG_PATH}" | awk '{print $1}')"
  fi
  SIZE="$(wc -c < "${PKG_PATH}" | tr -d ' ')"

  # manifest.json：合并本架构条目（同 OUT_DIR 多架构累积进同一 manifest），结构对齐 prepare.rs::ReelManifest。
  local MANIFEST="${OUT_DIR}/manifest.json"
  local PKG_URL="${BASE_URL:+${BASE_URL%/}/${PKG_NAME}}"
  PKG_URL="${PKG_URL:-REPLACE_WITH_OBJECT_STORAGE_URL/${PKG_NAME}}"
  local PKG_KEY="reel/${VERSION}/${PKG_NAME}"
  MANIFEST="${MANIFEST}" OS_ARCH="${OS_ARCH}" VERSION="${VERSION}" \
  PKG_KEY="${PKG_KEY}" PKG_URL="${PKG_URL}" SHA256="${SHA256}" SIZE="${SIZE}" \
  python3 - <<'PY'
import json, os, sys

path = os.environ["MANIFEST"]
os_arch = os.environ["OS_ARCH"]
version = os.environ["VERSION"]
entry = {
    "key": os.environ["PKG_KEY"],
    "url": os.environ["PKG_URL"],
    "sha256": os.environ["SHA256"],
    "size": int(os.environ["SIZE"]),
}
data = {"version": version, "packages": {}}
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("version") and data["version"] != version:
        sys.exit(f"manifest 版本冲突：已有 {data['version']}，本次 {version}（多架构须同版本）")
    data["version"] = version
    data.setdefault("packages", {})
data["packages"][os_arch] = entry
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print(f"[reel-pkg] manifest 写入 {path}（packages: {', '.join(sorted(data['packages']))}）")
PY

  echo "[reel-pkg] 完成 ${OS_ARCH}: ${PKG_PATH}（sha256=${SHA256:0:12}… size=${SIZE}）"
  if [[ "${PKG_URL}" == REPLACE_WITH_OBJECT_STORAGE_URL/* && -z "${PUBLISH_URL}" ]]; then
    echo "            ⚠ url 为占位：用 --base-url 重跑 manifest，或配 --publish 一键发布。"
  fi

  # 一键发布：POST 引擎包到云端 admin 端点（落公共桶 + 写 config_json.engine + push）。
  if [[ -n "${PUBLISH_URL}" ]]; then
    [[ "${NO_VENV}" == "1" ]] && echo "[reel-pkg] ⚠ --publish 配 --no-venv：上传的是 STRUCTURE-only 不可运行包，仅供 dev/test 验证链路，切勿指向生产！" >&2
    local ENDPOINT="${PUBLISH_URL%/}/api/v1/hasn/app-catalogs/${APP_PK}/engine-package"
    echo "[reel-pkg] 发布 → ${ENDPOINT}（os_arch=${OS_ARCH} version=${VERSION}）"
    # 服务端权威算 sha256（交叉校验）+ size，落公共桶，写 config_json.engine，push platform_config。
    # token 经 Authorization 头（不进 URL/日志）；-sS 静默但报错，-w 附 HTTP 码。
    local HTTP_BODY_FILE="${OUT_DIR}/.publish-resp-${OS_ARCH}.json" HTTP_CODE
    # Windows：mingw/msys curl 的 -F @file / -o 需原生 Windows 路径（MSYS 不会转 -F 里内嵌的 @路径，
    # 否则 curl 报 (26) Failed to open/read local data）。用 cygpath -m 转成 D:/… 前斜杠 Windows 路径。
    local CURL_PKG_PATH="${PKG_PATH}" CURL_BODY_FILE="${HTTP_BODY_FILE}"
    if [[ "${OS_KEY}" == "win" ]] && command -v cygpath >/dev/null 2>&1; then
      CURL_PKG_PATH="$(cygpath -m "${PKG_PATH}")"
      CURL_BODY_FILE="$(cygpath -m "${HTTP_BODY_FILE}")"
    fi
    HTTP_CODE="$(curl -sS -o "${CURL_BODY_FILE}" -w '%{http_code}' \
      -X POST "${ENDPOINT}" \
      -H "Authorization: Bearer ${ADMIN_TOKEN}" \
      -F "file=@${CURL_PKG_PATH};type=application/zip" \
      -F "os_arch=${OS_ARCH}" \
      -F "version=${VERSION}" \
      -F "sha256=${SHA256}" || echo "000")"
    if [[ "${HTTP_CODE}" != "200" ]]; then
      echo "[reel-pkg] ✗ 发布失败（HTTP ${HTTP_CODE}）：" >&2
      cat "${HTTP_BODY_FILE}" >&2 2>/dev/null || true
      echo >&2
      rm -f "${HTTP_BODY_FILE}"
      exit 1
    fi
    # 解析统一信封 {code,msg,data}：code 非 0 即业务失败；data 为写入后的 engine 配置。
    RESP_FILE="${HTTP_BODY_FILE}" OS_ARCH="${OS_ARCH}" python3 - <<'PY' || exit 1
import json, os, sys

with open(os.environ["RESP_FILE"], encoding="utf-8") as fh:
    env = json.load(fh)
code = env.get("code")
if code not in (0, 200):
    print(f"[reel-pkg] ✗ 云端业务失败 code={code} msg={env.get('msg')}", file=sys.stderr)
    sys.exit(1)
engine = env.get("data") or {}
pkgs = engine.get("packages") or {}
print(f"[reel-pkg] ✓ 已发布 {os.environ['OS_ARCH']}。云端 engine.version={engine.get('version')} "
      f"packages={', '.join(sorted(pkgs))}")
PY
    rm -f "${HTTP_BODY_FILE}"
  fi
}

# ---- 主流程：清旧 manifest（本次产一份干净的；跨机累积由云端 publish 服务端合并）+ 遍历目标 ----
mkdir -p "${OUT_DIR}"
rm -f "${OUT_DIR}/manifest.json"
for arch in ${TARGETS}; do
  process_one_arch "${arch}"
done

echo
echo "[reel-pkg] ✅ 全部完成：架构 [${TARGETS}] 版本 ${VERSION}。manifest: ${OUT_DIR}/manifest.json"
if [[ -n "${PUBLISH_URL}" ]]; then
  echo "[reel-pkg] 已发布到云端并 push platform_config —— 在线 daemon 将秒级重拉并自动安装引擎。"
  echo "[reel-pkg] 跨 OS（linux/win）请在对应机器上跑同一脚本同一配置，云端按 os-arch 累积进同一 manifest。"
fi
