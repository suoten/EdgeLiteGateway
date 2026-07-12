"""Comprehensive unit tests for edgelite.services.ai_service.AiModelService.

Covers: model listing, detail retrieval, update (incl. pipeline rebuild),
delete, enable/disable, reload, inference + async log flushing, stats,
inference summary, logs pagination, custom model registration, and shutdown.

The AI inference engine, HTTP layer and DB sessions are mocked. The
ai_preprocess/ai_postprocess modules are referenced via lazy imports inside
update_model but do not exist on disk, so a fixture injects fake modules into
sys.modules for the pipeline-rebuild tests.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.engine.edge_ai_inference import InferenceResult  # noqa: E402
from edgelite.services.ai_service import AiModelService  # noqa: E402


# -- Helpers -----------------------------------------------------------------


def make_wrapper(
    model_id="m1",
    model_name="Model-1",
    status="active",
    is_preset=False,
    model_version="v1.0.0",
    model_type="anomaly",
    model_path="/models/m1.onnx",
    input_schema=None,
    output_schema=None,
    loaded_at=None,
    preprocess_config=None,
    postprocess_config=None,
    batch_size=1,
    max_concurrent=4,
    timeout_ms=30000,
    device_preference="auto",
):
    w = MagicMock()
    w.model_id = model_id
    w.model_name = model_name
    w.model_version = model_version
    w.model_type = model_type
    w.model_path = model_path
    w.status = status
    w.is_preset = is_preset
    w.input_schema = input_schema if input_schema is not None else {"shape": [1, 3]}
    w.output_schema = output_schema if output_schema is not None else {"shape": [1]}
    w.loaded_at = loaded_at if loaded_at is not None else datetime.now(UTC)
    w.preprocess_config = preprocess_config or []
    w.postprocess_config = postprocess_config or []
    w.batch_size = batch_size
    w.max_concurrent = max_concurrent
    w.timeout_ms = timeout_ms
    w.device_preference = device_preference
    w._preprocess_pipeline = None
    w._postprocess_pipeline = None
    return w


def make_engine(models=None):
    engine = MagicMock()
    engine.get_loaded_models.return_value = models if models is not None else {}
    engine.get_model_stats.return_value = None
    engine.get_model.return_value = None
    engine.get_stats.return_value = {
        "total_calls": 0,
        "total_errors": 0,
        "avg_latency_ms": 0,
        "model_distribution": {},
    }
    engine.get_scheduled_inferences.return_value = []
    engine.infer = AsyncMock()
    engine.enable_model = AsyncMock(return_value=(True, ""))
    engine.disable_model = AsyncMock()
    engine.remove_model = AsyncMock()
    engine.reload_model = AsyncMock()
    engine.load_custom_model = AsyncMock()
    return engine


def make_db_session():
    db = MagicMock()
    session = AsyncMock()
    session.add = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)
    return db, session


def make_result(model_id="m1", status="success", latency_ms=5, output_data=None, error_message=None):
    return InferenceResult(
        model_id=model_id,
        output_data=output_data if output_data is not None else {"score": 0.1},
        latency_ms=latency_ms,
        status=status,
        error_message=error_message,
    )


def make_log(model_id="m1", status="success", latency_ms=10, idx=0):
    return {
        "log_id": "log-%d" % idx,
        "model_id": model_id,
        "model_name": "Model-%s" % model_id,
        "device_id": "dev1",
        "point_name": "p1",
        "input_summary": "[1.0]",
        "output_summary": '{"score": 0.1}',
        "latency_ms": latency_ms,
        "status": status,
        "error_message": None if status == "success" else "boom",
        "timestamp": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
def fake_pipeline_modules():
    pre_mod = types.ModuleType("edgelite.engine.ai_preprocess")
    post_mod = types.ModuleType("edgelite.engine.ai_postprocess")
    pre_pipe = MagicMock(return_value="PRE_PIPE")
    post_pipe = MagicMock(return_value="POST_PIPE")
    pre_mod.PreprocessPipeline = pre_pipe
    post_mod.PostprocessPipeline = post_pipe
    with patch.dict(
        sys.modules,
        {
            "edgelite.engine.ai_preprocess": pre_mod,
            "edgelite.engine.ai_postprocess": post_mod,
        },
    ):
        yield pre_pipe, post_pipe


# -- Init --------------------------------------------------------------------


class TestInit:
    def test_default_attributes(self):
        engine = make_engine()
        svc = AiModelService(engine)
        assert svc._engine is engine
        assert svc._database is None
        assert svc._inference_logs == []
        assert svc._pending_db_logs == []
        assert svc._max_logs == 1000
        assert svc._log_write_threshold == 3
        assert svc._db_counts == {}
        assert svc._flush_task is None
        assert svc._log_lock is not None

    def test_accepts_database(self):
        db = MagicMock()
        svc = AiModelService(make_engine(), database=db)
        assert svc._database is db


# -- restore_stats_from_db ---------------------------------------------------


class TestRestoreStatsFromDb:
    async def test_no_database_returns_early(self):
        svc = AiModelService(make_engine(), database=None)
        await svc.restore_stats_from_db()
        assert svc._db_counts == {}

    async def test_restores_counts_from_db(self):
        db, session = make_db_session()
        r_total = MagicMock()
        r_total.scalar.return_value = 8
        session.execute.side_effect = [
            [("m1", 5), ("m2", 3)],
            r_total,
            [("m1", 1)],
            [("m1", 12.5), ("m2", 7.0)],
        ]
        svc = AiModelService(make_engine(), database=db)
        await svc.restore_stats_from_db()
        assert svc._db_counts == {"m1": 5, "m2": 3}
        assert svc._db_total_count == 8
        assert svc._db_error_counts == {"m1": 1}
        assert svc._db_avg_latency == {"m1": 12.5, "m2": 7.0}

    async def test_empty_db_counts_skips_info_log(self):
        db, session = make_db_session()
        r_total = MagicMock()
        r_total.scalar.return_value = 0
        session.execute.side_effect = [[], r_total, [], []]
        svc = AiModelService(make_engine(), database=db)
        await svc.restore_stats_from_db()
        assert svc._db_counts == {}
        assert svc._db_total_count == 0

    async def test_scalar_none_defaults_to_zero(self):
        db, session = make_db_session()
        r_total = MagicMock()
        r_total.scalar.return_value = None
        session.execute.side_effect = [[], r_total, [], []]
        svc = AiModelService(make_engine(), database=db)
        await svc.restore_stats_from_db()
        assert svc._db_total_count == 0

    async def test_exception_resets_counts_and_preserves_others(self):
        db, session = make_db_session()
        session.execute.side_effect = RuntimeError("db down")
        svc = AiModelService(make_engine(), database=db)
        svc._db_counts = {"m1": 5}
        svc._db_error_counts = {"m1": 1}
        svc._db_avg_latency = {"m1": 10.0}
        svc._db_total_count = 5
        await svc.restore_stats_from_db()
        assert svc._db_counts == {}
        assert svc._db_error_counts == {"m1": 1}
        assert svc._db_avg_latency == {"m1": 10.0}
        assert svc._db_total_count == 5

    async def test_exception_when_no_prior_state_uses_defaults(self):
        db, session = make_db_session()
        session.execute.side_effect = RuntimeError("db down")
        svc = AiModelService(make_engine(), database=db)
        await svc.restore_stats_from_db()
        assert svc._db_counts == {}
        assert svc._db_error_counts == {}
        assert svc._db_avg_latency == {}
        assert svc._db_total_count == 0


# -- list_models -------------------------------------------------------------


class TestListModels:
    async def test_empty_models(self):
        svc = AiModelService(make_engine(models={}))
        result = await svc.list_models()
        assert result == {"items": [], "total": 0, "page": 1, "page_size": 20}

    async def test_lists_models_with_stats_and_db_counts(self):
        wrapper = make_wrapper()
        engine = make_engine(models={"m1": wrapper})
        engine.get_model_stats.return_value = {
            "call_count": 3,
            "error_count": 1,
            "avg_latency_ms": 10,
        }
        svc = AiModelService(engine)
        svc._db_counts = {"m1": 5}
        svc._db_error_counts = {"m1": 2}
        svc._db_avg_latency = {"m1": 12.0}
        result = await svc.list_models()
        assert result["total"] == 1
        item = result["items"][0]
        assert item["model_id"] == "m1"
        assert item["inference_count"] == 8
        assert item["error_count"] == 3
        assert item["avg_latency_ms"] == 10
        assert item["is_preset"] is False
        assert item["created_at"] == wrapper.loaded_at.isoformat()

    async def test_stats_none_uses_db_counts(self):
        wrapper = make_wrapper()
        engine = make_engine(models={"m1": wrapper})
        engine.get_model_stats.return_value = None
        svc = AiModelService(engine)
        svc._db_counts = {"m1": 5}
        svc._db_error_counts = {"m1": 2}
        svc._db_avg_latency = {"m1": 12.0}
        result = await svc.list_models()
        item = result["items"][0]
        assert item["inference_count"] == 5
        assert item["error_count"] == 2
        assert item["avg_latency_ms"] == 12

    async def test_pagination(self):
        models = {"m%d" % i: make_wrapper(model_id="m%d" % i) for i in range(5)}
        engine = make_engine(models=models)
        engine.get_model_stats.return_value = None
        svc = AiModelService(engine)
        page1 = await svc.list_models(page=1, page_size=2)
        assert len(page1["items"]) == 2
        assert page1["total"] == 5
        page2 = await svc.list_models(page=2, page_size=2)
        assert len(page2["items"]) == 2
        page3 = await svc.list_models(page=3, page_size=2)
        assert len(page3["items"]) == 1

    async def test_page_zero_and_oversize_page_clamped(self):
        models = {"m%d" % i: make_wrapper(model_id="m%d" % i) for i in range(3)}
        engine = make_engine(models=models)
        engine.get_model_stats.return_value = None
        svc = AiModelService(engine)
        result = await svc.list_models(page=0, page_size=500)
        assert result["page"] == 1
        assert result["page_size"] == 200
        assert len(result["items"]) == 3


# -- get_model ---------------------------------------------------------------


class TestGetModel:
    async def test_not_found_returns_none(self):
        engine = make_engine()
        engine.get_model.return_value = None
        svc = AiModelService(engine)
        assert await svc.get_model("missing") is None

    async def test_returns_detail_with_stats(self):
        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        engine.get_model_stats.return_value = {
            "call_count": 4,
            "error_count": 1,
            "avg_latency_ms": 9,
        }
        svc = AiModelService(engine)
        svc._db_counts = {"m1": 6}
        svc._db_error_counts = {"m1": 2}
        detail = await svc.get_model("m1")
        assert detail is not None
        assert detail.model_id == "m1"
        assert detail.inference_count == 10
        assert detail.error_count == 3
        assert detail.avg_latency_ms == 9
        assert detail.last_inference_at is None

    async def test_stats_none_uses_db_avg_latency(self):
        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        engine.get_model_stats.return_value = None
        svc = AiModelService(engine)
        svc._db_avg_latency = {"m1": 15.7}
        detail = await svc.get_model("m1")
        assert detail.avg_latency_ms == 15


# -- update_model ------------------------------------------------------------


class TestUpdateModel:
    async def test_not_found_returns_none(self):
        engine = make_engine()
        engine.get_model.return_value = None
        svc = AiModelService(engine)
        assert await svc.update_model("missing", {"model_name": "x"}) is None

    async def test_updates_name_and_schemas(self):
        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        svc = AiModelService(engine)
        result = await svc.update_model(
            "m1",
            {
                "model_name": "Renamed",
                "input_schema": {"shape": [2]},
                "output_schema": {"shape": [3]},
            },
        )
        assert wrapper.model_name == "Renamed"
        assert wrapper.input_schema == {"shape": [2]}
        assert wrapper.output_schema == {"shape": [3]}
        assert result is not None
        assert result.model_name == "Renamed"

    async def test_preprocess_config_rebuilds_pipeline(self, fake_pipeline_modules):
        pre_pipe, _ = fake_pipeline_modules
        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        svc = AiModelService(engine)
        await svc.update_model("m1", {"preprocess_config": [{"type": "normalize"}]})
        pre_pipe.assert_called_once_with([{"type": "normalize"}])
        assert wrapper.preprocess_config == [{"type": "normalize"}]
        assert wrapper._preprocess_pipeline == "PRE_PIPE"

    async def test_postprocess_config_rebuilds_pipeline(self, fake_pipeline_modules):
        _, post_pipe = fake_pipeline_modules
        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        svc = AiModelService(engine)
        await svc.update_model("m1", {"postprocess_config": [{"type": "softmax"}]})
        post_pipe.assert_called_once_with([{"type": "softmax"}])
        assert wrapper.postprocess_config == [{"type": "softmax"}]
        assert wrapper._postprocess_pipeline == "POST_PIPE"

    async def test_updates_runtime_fields(self):
        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        svc = AiModelService(engine)
        await svc.update_model(
            "m1",
            {
                "batch_size": 8,
                "max_concurrent": 16,
                "timeout_ms": 60000,
                "device_preference": "cuda",
            },
        )
        assert wrapper.batch_size == 8
        assert wrapper.max_concurrent == 16
        assert wrapper.timeout_ms == 60000
        assert wrapper.device_preference == "cuda"

    async def test_empty_update_data_keeps_fields(self):
        wrapper = make_wrapper(model_name="Original")
        engine = make_engine()
        engine.get_model.return_value = wrapper
        svc = AiModelService(engine)
        result = await svc.update_model("m1", {})
        assert wrapper.model_name == "Original"
        assert result is not None
        assert result.model_name == "Original"


# -- delete_model ------------------------------------------------------------


class TestDeleteModel:
    async def test_not_found_returns_false(self):
        engine = make_engine()
        engine.get_model.return_value = None
        svc = AiModelService(engine)
        assert await svc.delete_model("missing") is False

    async def test_preset_model_returns_false(self):
        wrapper = make_wrapper(is_preset=True)
        engine = make_engine()
        engine.get_model.return_value = wrapper
        svc = AiModelService(engine)
        assert await svc.delete_model("m1") is False
        engine.remove_model.assert_not_called()

    async def test_custom_model_removed(self):
        wrapper = make_wrapper(is_preset=False)
        engine = make_engine()
        engine.get_model.return_value = wrapper
        svc = AiModelService(engine)
        assert await svc.delete_model("m1") is True
        engine.remove_model.assert_awaited_once_with("m1")


# -- enable_model ------------------------------------------------------------


class TestEnableModel:
    async def test_not_found_returns_false(self):
        engine = make_engine()
        engine.get_model.return_value = None
        svc = AiModelService(engine)
        assert await svc.enable_model("missing") is False

    @patch("edgelite.engine.edge_ai_inference._check_onnxruntime", return_value=False)
    async def test_onnxruntime_missing_raises_503(self, _mock):
        from fastapi import HTTPException

        wrapper = make_wrapper(status="inactive")
        engine = make_engine()
        engine.get_model.return_value = wrapper
        svc = AiModelService(engine)
        with pytest.raises(HTTPException) as exc:
            await svc.enable_model("m1")
        assert exc.value.status_code == 503
        assert wrapper.status == "inactive"

    @patch("edgelite.engine.edge_ai_inference._check_onnxruntime", return_value=True)
    async def test_engine_returns_onnxruntime_error_raises_503(self, _mock):
        from fastapi import HTTPException

        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        engine.enable_model = AsyncMock(return_value=(False, "ERR_AI_ONNXRUNTIME_NOT_INSTALLED"))
        svc = AiModelService(engine)
        with pytest.raises(HTTPException) as exc:
            await svc.enable_model("m1")
        assert exc.value.status_code == 503

    @patch("edgelite.engine.edge_ai_inference._check_onnxruntime", return_value=True)
    async def test_engine_returns_other_reason_raises_400(self, _mock):
        from fastapi import HTTPException

        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        engine.enable_model = AsyncMock(return_value=(False, "ERR_AI_MODEL_NOT_FOUND"))
        svc = AiModelService(engine)
        with pytest.raises(HTTPException) as exc:
            await svc.enable_model("m1")
        assert exc.value.status_code == 400
        assert "ERR_AI_MODEL_NOT_FOUND" in exc.value.detail

    @patch("edgelite.engine.edge_ai_inference._check_onnxruntime", return_value=True)
    async def test_engine_returns_false_without_reason(self, _mock):
        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        engine.enable_model = AsyncMock(return_value=(False, ""))
        svc = AiModelService(engine)
        assert await svc.enable_model("m1") is False

    @patch("edgelite.engine.edge_ai_inference._check_onnxruntime", return_value=True)
    async def test_engine_returns_true(self, _mock):
        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        engine.enable_model = AsyncMock(return_value=(True, ""))
        svc = AiModelService(engine)
        assert await svc.enable_model("m1") is True


# -- disable_model -----------------------------------------------------------


class TestDisableModel:
    async def test_not_found_returns_false(self):
        engine = make_engine()
        engine.get_model.return_value = None
        svc = AiModelService(engine)
        assert await svc.disable_model("missing") is False

    async def test_disables_model(self):
        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        svc = AiModelService(engine)
        assert await svc.disable_model("m1") is True
        engine.disable_model.assert_awaited_once_with("m1")


# -- reload_model ------------------------------------------------------------


class TestReloadModel:
    async def test_success_returns_true(self):
        engine = make_engine()
        engine.reload_model = AsyncMock()
        svc = AiModelService(engine)
        assert await svc.reload_model("m1", "/models/m1.onnx") is True
        engine.reload_model.assert_awaited_once_with("m1", "/models/m1.onnx")

    async def test_exception_returns_false(self):
        engine = make_engine()
        engine.reload_model = AsyncMock(side_effect=RuntimeError("boom"))
        svc = AiModelService(engine)
        assert await svc.reload_model("m1", "/models/m1.onnx") is False


# -- inference ---------------------------------------------------------------


class TestInference:
    async def test_basic_inference_returns_log(self):
        wrapper = make_wrapper(model_name="AnomalyModel")
        engine = make_engine()
        engine.get_model.return_value = wrapper
        engine.infer = AsyncMock(return_value=make_result(latency_ms=7))
        svc = AiModelService(engine)
        result = await svc.inference("m1", [1.0, 2.0, 3.0], device_id="dev1", point_name="temp")
        assert result["model_id"] == "m1"
        assert result["latency_ms"] == 7
        assert result["status"] == "success"
        assert result["log"]["model_name"] == "AnomalyModel"
        assert result["log"]["device_id"] == "dev1"
        assert result["log"]["point_name"] == "temp"
        assert result["log"]["input_summary"] == "[1.0, 2.0, 3.0]"
        assert len(svc._inference_logs) == 1

    async def test_long_input_summary_truncated(self):
        wrapper = make_wrapper()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        engine.infer = AsyncMock(return_value=make_result())
        svc = AiModelService(engine)
        result = await svc.inference("m1", [float(i) for i in range(10)])
        assert result["log"]["input_summary"].endswith("...")
        assert result["log"]["input_summary"].startswith("[")

    async def test_inference_without_wrapper_uses_model_id_as_name(self):
        engine = make_engine()
        engine.get_model.return_value = None
        engine.infer = AsyncMock(return_value=make_result())
        svc = AiModelService(engine)
        result = await svc.inference("m1", [1.0])
        assert result["log"]["model_name"] == "m1"

    async def test_error_status_recorded_in_log(self):
        engine = make_engine()
        engine.get_model.return_value = make_wrapper()
        engine.infer = AsyncMock(
            return_value=make_result(status="error", error_message="infer failed")
        )
        svc = AiModelService(engine)
        result = await svc.inference("m1", [1.0])
        assert result["status"] == "error"
        assert result["log"]["error_message"] == "infer failed"

    async def test_inference_triggers_flush_task(self):
        wrapper = make_wrapper()
        db, session = make_db_session()
        engine = make_engine()
        engine.get_model.return_value = wrapper
        engine.infer = AsyncMock(return_value=make_result())
        svc = AiModelService(engine, database=db)
        svc._log_write_threshold = 3
        for _ in range(3):
            await svc.inference("m1", [1.0, 2.0])
        assert svc._flush_task is not None
        await svc._flush_task
        assert session.add.call_count == 3
        session.commit.assert_awaited_once()
        assert svc._pending_db_logs == []

    async def test_inference_logs_truncated_at_max(self):
        engine = make_engine()
        engine.get_model.return_value = make_wrapper()
        engine.infer = AsyncMock(return_value=make_result())
        svc = AiModelService(engine)
        svc._max_logs = 3
        svc._log_write_threshold = 999
        for _ in range(5):
            await svc.inference("m1", [1.0])
        assert len(svc._inference_logs) == 3


# -- _flush_logs_to_db -------------------------------------------------------


class TestFlushLogsToDb:
    async def test_no_database_returns_early(self):
        svc = AiModelService(make_engine(), database=None)
        svc._pending_db_logs = [make_log()]
        await svc._flush_logs_to_db()
        assert len(svc._pending_db_logs) == 1

    async def test_no_pending_logs_returns_early(self):
        db, session = make_db_session()
        svc = AiModelService(make_engine(), database=db)
        await svc._flush_logs_to_db()
        session.add.assert_not_called()
        session.commit.assert_not_called()

    async def test_successful_flush_writes_and_clears(self):
        db, session = make_db_session()
        svc = AiModelService(make_engine(), database=db)
        svc._pending_db_logs = [make_log(idx=1), make_log(idx=2)]
        await svc._flush_logs_to_db()
        assert session.add.call_count == 2
        session.commit.assert_awaited_once()
        assert svc._pending_db_logs == []

    async def test_commit_failure_re_enqueues_logs(self):
        db, session = make_db_session()
        session.commit.side_effect = RuntimeError("commit failed")
        svc = AiModelService(make_engine(), database=db)
        svc._pending_db_logs = [make_log(idx=1), make_log(idx=2)]
        await svc._flush_logs_to_db()
        assert len(svc._pending_db_logs) == 2
        assert svc._pending_db_logs[0]["log_id"] == "log-1"

    async def test_commit_failure_truncates_oversized_reenqueue(self):
        db, session = make_db_session()
        session.commit.side_effect = RuntimeError("commit failed")
        svc = AiModelService(make_engine(), database=db)
        svc._max_logs = 3
        svc._pending_db_logs = [make_log(idx=i) for i in range(6)]
        await svc._flush_logs_to_db()
        assert len(svc._pending_db_logs) == 3
        assert svc._pending_db_logs[0]["log_id"] == "log-0"


# -- get_stats ---------------------------------------------------------------


class TestGetStats:
    async def test_stats_merge_engine_and_db(self):
        models = {"m1": make_wrapper(), "m2": make_wrapper(model_id="m2")}
        engine = make_engine(models=models)
        engine.get_stats.return_value = {
            "total_calls": 10,
            "total_errors": 2,
            "avg_latency_ms": 15,
            "model_distribution": {"m1": 6, "m2": 4},
        }
        svc = AiModelService(engine)
        svc._db_counts = {"m1": 5}
        svc._db_error_counts = {"m1": 3}
        stats = await svc.get_stats()
        assert stats.model_count == 2
        assert stats.total_calls == 15
        assert stats.total_errors == 5
        assert stats.avg_latency_ms == 15
        assert stats.model_distribution == {"m1": 6, "m2": 4}


# -- get_inference_summary ---------------------------------------------------


class TestGetInferenceSummary:
    async def test_empty_summary(self):
        engine = make_engine(models={})
        svc = AiModelService(engine)
        summary = await svc.get_inference_summary()
        assert summary["model_count"] == 0
        assert summary["active_model_count"] == 0
        assert summary["total_calls"] == 0
        assert summary["recent_inferences"] == []
        assert summary["latency_trend"] == []
        assert summary["anomaly_count"] == 0
        assert summary["active_schedule_count"] == 0

    async def test_summary_with_logs_and_active_models(self):
        models = {
            "m1": make_wrapper(status="active"),
            "m2": make_wrapper(model_id="m2", status="inactive"),
        }
        engine = make_engine(models=models)
        engine.get_stats.return_value = {
            "total_calls": 10,
            "total_errors": 1,
            "avg_latency_ms": 12,
            "model_distribution": {"m1": 10},
        }
        engine.get_scheduled_inferences.return_value = [{"id": "s1"}, {"id": "s2"}]
        svc = AiModelService(engine)
        logs = []
        for i in range(12):
            logs.append(
                make_log(model_id="m1", status="success" if i % 3 else "error", latency_ms=10 + i, idx=i)
            )
        svc._inference_logs = logs
        summary = await svc.get_inference_summary()
        assert summary["model_count"] == 2
        assert summary["active_model_count"] == 1
        assert summary["total_calls"] == 10
        assert summary["active_schedule_count"] == 2
        assert len(summary["recent_inferences"]) == 10
        assert summary["recent_inferences"][-1]["latency_ms"] == 21
        assert all(item["v"] >= 10 for item in summary["latency_trend"])
        assert summary["anomaly_count"] > 0

    async def test_summary_recent_capped_at_ten(self):
        engine = make_engine()
        svc = AiModelService(engine)
        svc._inference_logs = [make_log(idx=i) for i in range(15)]
        summary = await svc.get_inference_summary()
        assert len(summary["recent_inferences"]) == 10


# -- get_model_stats ---------------------------------------------------------


class TestGetModelStatsService:
    async def test_delegates_to_engine(self):
        engine = make_engine()
        engine.get_model_stats.return_value = {"call_count": 5}
        svc = AiModelService(engine)
        assert await svc.get_model_stats("m1") == {"call_count": 5}

    async def test_delegates_none(self):
        engine = make_engine()
        engine.get_model_stats.return_value = None
        svc = AiModelService(engine)
        assert await svc.get_model_stats("m1") is None


# -- get_inference_logs ------------------------------------------------------


class TestGetInferenceLogs:
    async def test_no_filter_paginates(self):
        svc = AiModelService(make_engine())
        svc._inference_logs = [make_log(idx=i) for i in range(5)]
        result = await svc.get_inference_logs(page=1, page_size=2)
        assert result["total"] == 5
        assert len(result["items"]) == 2
        assert result["page"] == 1

    async def test_filter_by_model_id(self):
        svc = AiModelService(make_engine())
        svc._inference_logs = [
            make_log(model_id="m1", idx=0),
            make_log(model_id="m2", idx=1),
            make_log(model_id="m1", idx=2),
        ]
        result = await svc.get_inference_logs(model_id="m1")
        assert result["total"] == 2
        assert all(item["model_id"] == "m1" for item in result["items"])

    async def test_second_page(self):
        svc = AiModelService(make_engine())
        svc._inference_logs = [make_log(idx=i) for i in range(5)]
        result = await svc.get_inference_logs(page=2, page_size=2)
        assert len(result["items"]) == 2
        assert result["items"][0]["log_id"] == "log-2"


# -- register_uploaded_model -------------------------------------------------


class TestRegisterUploadedModel:
    async def test_registers_onnx_model(self):
        wrapper = make_wrapper()
        engine = make_engine()
        engine.load_custom_model = AsyncMock(return_value=wrapper)
        svc = AiModelService(engine)
        model_id = await svc.register_uploaded_model("MyModel", "/uploads/m.onnx")
        assert model_id.startswith("custom_MyModel_")
        engine.load_custom_model.assert_awaited_once()
        kwargs = engine.load_custom_model.await_args.kwargs
        assert kwargs["model_type"] == "onnx"
        assert kwargs["model_version"] == "v1.0.0"
        assert kwargs["model_name"] == "MyModel"

    async def test_registers_tflite_model(self):
        engine = make_engine()
        engine.load_custom_model = AsyncMock(return_value=make_wrapper())
        svc = AiModelService(engine)
        await svc.register_uploaded_model("T", "/uploads/m.tflite")
        assert engine.load_custom_model.await_args.kwargs["model_type"] == "tflite"

    async def test_registers_pmml_model(self):
        engine = make_engine()
        engine.load_custom_model = AsyncMock(return_value=make_wrapper())
        svc = AiModelService(engine)
        await svc.register_uploaded_model("P", "/uploads/m.pmml")
        assert engine.load_custom_model.await_args.kwargs["model_type"] == "pmml"

    async def test_unknown_extension_defaults_to_onnx(self):
        engine = make_engine()
        engine.load_custom_model = AsyncMock(return_value=make_wrapper())
        svc = AiModelService(engine)
        await svc.register_uploaded_model("X", "/uploads/m.txt")
        assert engine.load_custom_model.await_args.kwargs["model_type"] == "onnx"

    async def test_load_returns_none_raises_runtime_error(self):
        engine = make_engine()
        engine.load_custom_model = AsyncMock(return_value=None)
        svc = AiModelService(engine)
        with pytest.raises(RuntimeError, match="Failed to load model"):
            await svc.register_uploaded_model("Bad", "/uploads/m.onnx")


# -- shutdown ----------------------------------------------------------------


class TestShutdown:
    async def test_no_pending_logs_skips_flush(self):
        db, session = make_db_session()
        svc = AiModelService(make_engine(), database=db)
        await svc.shutdown()
        session.commit.assert_not_called()

    async def test_pending_logs_flushed_on_shutdown(self):
        db, session = make_db_session()
        svc = AiModelService(make_engine(), database=db)
        svc._pending_db_logs = [make_log(idx=1), make_log(idx=2)]
        await svc.shutdown()
        assert session.add.call_count == 2
        session.commit.assert_awaited_once()
        assert svc._pending_db_logs == []

    async def test_shutdown_without_database_no_flush(self):
        svc = AiModelService(make_engine(), database=None)
        svc._pending_db_logs = [make_log()]
        await svc.shutdown()
        assert len(svc._pending_db_logs) == 1
