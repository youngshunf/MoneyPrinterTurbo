"""唤星 reel fork 改造的回归测试（零 fake，doc19 P1）。

覆盖：
  ① REEL_* env override 生效（设 env → config 反映）；
  ② 安全闸（无 X-Reel-Token → 403，正确 token → 放行；Host 闸）；
  ③ POST /api/v1/reel/materials/search 入出参 shape（mock 搜索，不联网）；
  ④ §6.6 缺口计算纯函数 + auto 分支（mock 素材/下载，不真下载）。

真实 LLM/Pexels/平台 TTS 为 infra-gated，不在此覆盖。
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.api.huanxing_security import apply_huanxing_hardening
from app.config import config
from app.controllers.v1 import reel as reel_ctrl
from app.models.schema import MaterialInfo, VideoParams
from app.services import task as tm
from app.utils import utils


# ─────────────────────────── ① env override ───────────────────────────
class TestReelRuntimeEnvOverride(unittest.TestCase):
    """REEL_* env 注入覆盖 config（apply_reel_runtime_env）。"""

    def setUp(self):
        self._orig_app = dict(config.app)
        self._orig_whisper = dict(config.whisper)
        self._orig_env = dict(os.environ)

    def tearDown(self):
        config.app.clear()
        config.app.update(self._orig_app)
        config.whisper.clear()
        config.whisper.update(self._orig_whisper)
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_gateway_and_model_env_override_config(self):
        os.environ["REEL_GATEWAY_BASE_URL"] = "https://gw.example/v1"
        os.environ["REEL_GATEWAY_API_KEY"] = "sk-reel-test"
        os.environ["REEL_MODEL_LLM"] = "agnes-2.0-flash"
        os.environ["REEL_MODEL_LLM_FALLBACKS"] = "model-b, model-c"

        config.apply_reel_runtime_env()

        self.assertEqual(config.app["llm_provider"], "openai")
        self.assertEqual(config.app["openai_base_url"], "https://gw.example/v1")
        self.assertEqual(config.app["openai_api_key"], "sk-reel-test")
        self.assertEqual(config.app["openai_model_name"], "agnes-2.0-flash")
        self.assertEqual(config.reel_llm_model_fallbacks(), ["model-b", "model-c"])
        self.assertEqual(config.gateway_credentials(), ("https://gw.example/v1", "sk-reel-test"))

    def test_material_keys_and_tts_subtitle_env_override(self):
        os.environ["REEL_PEXELS_API_KEYS"] = "px1, px2 , px3"
        os.environ["REEL_PIXABAY_API_KEYS"] = "pb1"
        os.environ["REEL_TTS_PROVIDER"] = "platform"
        os.environ["REEL_SUBTITLE_PROVIDER"] = "whisper"
        os.environ["REEL_WHISPER_MODEL_SIZE"] = "large-v3"

        config.apply_reel_runtime_env()

        self.assertEqual(config.app["pexels_api_keys"], ["px1", "px2", "px3"])
        self.assertEqual(config.app["pixabay_api_keys"], ["pb1"])
        self.assertEqual(config.app["reel_tts_provider"], "platform")
        self.assertEqual(config.app["subtitle_provider"], "whisper")
        self.assertEqual(config.whisper["model_size"], "large-v3")

    def test_upload_post_hardcoded_off(self):
        # 即便 env 不动，apply_reel_runtime_env 也必须把第三方发布关掉。
        config.apply_reel_runtime_env()
        self.assertFalse(config.app["upload_post_enabled"])
        self.assertFalse(config.app["upload_post_auto_upload"])

    def test_no_gateway_env_leaves_provider_untouched(self):
        for k in ("REEL_GATEWAY_BASE_URL", "REEL_GATEWAY_API_KEY"):
            os.environ.pop(k, None)
        config.app["llm_provider"] = "deepseek"  # 模拟 config.toml 原值
        config.apply_reel_runtime_env()
        # 未注入网关时不强改 provider（gateway 短路才接管，见 llm._generate_response）。
        self.assertEqual(config.app["llm_provider"], "deepseek")
        self.assertIsNone(config.gateway_credentials())

    def test_media_root_redirects_storage(self):
        os.environ["REEL_MEDIA_ROOT"] = "/tmp/reel-unit-media-root"
        self.assertEqual(utils.reel_media_root(), "/tmp/reel-unit-media-root")
        self.assertTrue(utils.storage_dir("cache_videos").startswith("/tmp/reel-unit-media-root"))


# ─────────────────────────── ② 安全闸 ───────────────────────────
def _build_hardened_app(token: str = "", hosts: str = "") -> FastAPI:
    """构造最小 app + reel 安全加固，env-gated（隔离测试中间件，不重载全局 config）。"""
    app = FastAPI()
    # 挂真实健康路由（/ping、/health），验证 token 豁免对实际端点生效。
    app.include_router(reel_ctrl.health_router)

    @app.post("/api/v1/videos")
    def videos():
        return {"ok": True}

    @app.post("/api/v1/reel/materials/search")
    def search():
        return []

    env = {}
    if token:
        env["REEL_SIDECAR_TOKEN"] = token
    if hosts:
        env["REEL_ALLOWED_HOSTS"] = hosts
    with patch.dict(os.environ, env, clear=False):
        apply_huanxing_hardening(app)
    return app


class TestReelSecurityGate(unittest.TestCase):
    # base_url=localhost 让 TestClient 默认 Host 头进 loopback 白名单（否则 testserver 被 Host 闸 400）。
    def _client(self, token: str = "", hosts: str = "") -> TestClient:
        return TestClient(_build_hardened_app(token=token, hosts=hosts), base_url="http://localhost")

    def test_business_endpoint_rejected_without_token(self):
        resp = self._client(token="secret-token").post("/api/v1/videos")
        self.assertEqual(resp.status_code, 403)

    def test_business_endpoint_passes_with_correct_token(self):
        resp = self._client(token="secret-token").post(
            "/api/v1/videos", headers={"X-Reel-Token": "secret-token"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_reel_endpoint_rejected_without_token(self):
        resp = self._client(token="secret-token").post("/api/v1/reel/materials/search")
        self.assertEqual(resp.status_code, 403)

    def test_health_check_exempt_from_token(self):
        resp = self._client(token="secret-token").get("/ping")
        self.assertEqual(resp.status_code, 200)

    def test_health_alias_exempt_from_token(self):
        resp = self._client(token="secret-token").get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_no_token_configured_allows_all(self):
        self.assertEqual(self._client(token="").post("/api/v1/videos").status_code, 200)

    def test_host_gate_rejects_unlisted_host(self):
        app = _build_hardened_app(token="", hosts="127.0.0.1,localhost")
        client = TestClient(app)
        resp = client.post("/api/v1/videos", headers={"host": "evil.example"})
        self.assertEqual(resp.status_code, 400)  # TrustedHostMiddleware → 400 Invalid host

    def test_host_gate_allows_listed_host(self):
        app = _build_hardened_app(token="", hosts="127.0.0.1,localhost")
        client = TestClient(app)
        resp = client.post("/api/v1/videos", headers={"host": "localhost"})
        self.assertEqual(resp.status_code, 200)


# ─────────────────── ③ materials/search 入出参 shape ───────────────────
class TestReelMaterialSearchEndpoint(unittest.TestCase):
    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(reel_ctrl.router)
        return TestClient(app)

    def test_search_returns_candidate_shape(self):
        def fake_pexels(search_term, minimum_duration, video_aspect):
            m = MaterialInfo()
            m.provider = "pexels"
            m.url = f"https://pexels.example/{search_term}.mp4"
            m.duration = 12
            return [m]

        with patch.object(reel_ctrl.material, "search_videos_pexels", side_effect=fake_pexels):
            client = self._client()
            resp = client.post(
                "/api/v1/reel/materials/search",
                json={"search_terms": ["autumn drinks", "hot latte"], "video_source": "pexels"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 2)  # 两个不同 term → 两个候选，去重保留
        for item in body:
            self.assertEqual(set(item.keys()), {"provider", "url", "duration"})
            self.assertEqual(item["provider"], "pexels")
            self.assertIsInstance(item["duration"], int)

    def test_search_dedups_identical_urls(self):
        def fake(search_term, minimum_duration, video_aspect):
            m = MaterialInfo()
            m.provider = "pexels"
            m.url = "https://pexels.example/same.mp4"  # 同一 URL，跨 term 应去重
            m.duration = 9
            return [m]

        with patch.object(reel_ctrl.material, "search_videos_pexels", side_effect=fake):
            client = self._client()
            resp = client.post(
                "/api/v1/reel/materials/search",
                json={"search_terms": ["a", "b", "c"]},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_search_rejects_empty_terms(self):
        client = self._client()
        resp = client.post("/api/v1/reel/materials/search", json={"search_terms": []})
        self.assertEqual(resp.status_code, 422)  # pydantic min_length=1


class TestReelEventsEndpoint(unittest.TestCase):
    """JSONL 流式端点：完成→progress+stage_complete；失败→error；不存在→error。"""

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(reel_ctrl.router)
        return TestClient(app)

    def _lines(self, resp):
        return [
            __import__("json").loads(line)
            for line in resp.text.splitlines()
            if line.strip()
        ]

    def test_completed_task_emits_progress_and_stage_complete(self):
        from app.models import const

        task = {
            "state": const.TASK_STATE_COMPLETE,
            "progress": 100,
            "videos": ["/m/final-1.mp4"],
            "script": "hi",
        }
        with patch.object(reel_ctrl.sm.state, "get_task", return_value=task):
            resp = self._client().post("/api/v1/reel/tasks/abc/events")
        self.assertEqual(resp.status_code, 200)
        lines = self._lines(resp)
        types = [line["type"] for line in lines]
        self.assertIn("progress", types)
        self.assertIn("stage_complete", types)
        sc = next(line for line in lines if line["type"] == "stage_complete")
        self.assertEqual(sc["payload"]["videos"], ["/m/final-1.mp4"])
        self.assertEqual(sc["payload"]["script"], "hi")

    def test_failed_task_emits_error(self):
        from app.models import const

        task = {"state": const.TASK_STATE_FAILED, "progress": 40, "error": "boom"}
        with patch.object(reel_ctrl.sm.state, "get_task", return_value=task):
            resp = self._client().post("/api/v1/reel/tasks/abc/events")
        lines = self._lines(resp)
        self.assertEqual(lines[-1]["type"], "error")
        self.assertEqual(lines[-1]["message"], "boom")

    def test_missing_task_emits_error(self):
        with patch.object(reel_ctrl.sm.state, "get_task", return_value=None):
            resp = self._client().post("/api/v1/reel/tasks/missing/events")
        lines = self._lines(resp)
        self.assertEqual(lines[-1]["type"], "error")
        self.assertIn("not found", lines[-1]["message"])


# ─────────────────── ④ §6.6 缺口计算 + auto 分支 ───────────────────
class TestReelMaterialGapCompute(unittest.TestCase):
    def test_gap_zero_when_owned_covers(self):
        self.assertEqual(tm.compute_material_gap(covered_seconds=30, required_seconds=20), 0.0)

    def test_gap_positive_when_partial(self):
        self.assertEqual(tm.compute_material_gap(covered_seconds=12, required_seconds=20), 8.0)

    def test_gap_full_when_no_owned(self):
        self.assertEqual(tm.compute_material_gap(covered_seconds=0, required_seconds=15), 15.0)

    def test_gap_negative_inputs_clamped(self):
        self.assertEqual(tm.compute_material_gap(covered_seconds=-5, required_seconds=-3), 0.0)

    def test_covered_seconds_caps_each_clip_at_max(self):
        clips = [MaterialInfo(duration=20), MaterialInfo(duration=3), MaterialInfo(duration=0)]
        # clip1 capped at 5, clip2 = 3, clip3 (0 duration) → conservative full clip = 5 → 13.
        self.assertEqual(tm._covered_seconds(clips, max_clip_duration=5), 13.0)


class TestReelAutoSourceBranch(unittest.TestCase):
    """video_source=auto：自带足够不下载 / 缺口才补库存（mock 预处理与下载）。"""

    def _params(self, materials=None, count=1, clip=5):
        return VideoParams(
            video_subject="autumn",
            video_source="auto",
            video_materials=materials,
            video_count=count,
            video_clip_duration=clip,
        )

    def test_owned_sufficient_skips_stock_download(self):
        owned = [MaterialInfo(url="/m/a.mp4", duration=10), MaterialInfo(url="/m/b.mp4", duration=10)]
        with patch.object(tm.video, "preprocess_video", return_value=owned), patch.object(
            tm.material, "download_videos"
        ) as mock_dl:
            result = tm.get_video_materials(
                task_id="t1", params=self._params(materials=owned), video_terms=["x"], audio_duration=8
            )
        self.assertEqual(result, ["/m/a.mp4", "/m/b.mp4"])
        mock_dl.assert_not_called()  # owned 覆盖 10s >= 8s 需求 → 不下载

    def test_gap_triggers_stock_fill_concatenated_after_owned(self):
        owned = [MaterialInfo(url="/m/a.mp4", duration=4)]  # covers 4s
        with patch.object(tm.video, "preprocess_video", return_value=owned), patch.object(
            tm.material, "download_videos", return_value=["/cache/s1.mp4", "/cache/s2.mp4"]
        ) as mock_dl:
            result = tm.get_video_materials(
                task_id="t2", params=self._params(materials=owned), video_terms=["x"], audio_duration=15
            )
        # owned first, then stock fill.
        self.assertEqual(result, ["/m/a.mp4", "/cache/s1.mp4", "/cache/s2.mp4"])
        mock_dl.assert_called_once()
        # 只补缺口：required 15 - covered 4 = 11s 传给下载器。
        self.assertAlmostEqual(mock_dl.call_args.kwargs["audio_duration"], 11.0)

    def test_no_owned_full_stock(self):
        with patch.object(tm.video, "preprocess_video", return_value=[]), patch.object(
            tm.material, "download_videos", return_value=["/cache/s1.mp4"]
        ) as mock_dl:
            result = tm.get_video_materials(
                task_id="t3", params=self._params(materials=None), video_terms=["x"], audio_duration=10
            )
        self.assertEqual(result, ["/cache/s1.mp4"])
        self.assertAlmostEqual(mock_dl.call_args.kwargs["audio_duration"], 10.0)


if __name__ == "__main__":
    unittest.main()
