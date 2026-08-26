"""
AJL 安捷伦色谱仪 AAC HTTP 客户端（新版 9 步协议）

封装与色谱仪 AAC 系统交互的全部 API 步骤，供 AJLHandler 调用。

职责：只负责业务 API 组包与调用，通用 HTTP 通信由 network.http_client.HttpClient 提供。

步骤概览：
    step1_login               — 登录，获取访问权限
    step2_sync_samples        — 同步样品数据
    step3_query_instrument    — 通过第三方标识查询 cdsId 和 GCTray injectorId
    step4_ready_to_place      — 确认仪器可放样（可选但建议）
    step5_apply_position      — 申请进样器位置（触发机械动作）
    step6_place_complete      — 通知放样完成（触发归位）
    step7_start_analysis      — 启动分析，返回 analysisRunId
    step8_poll_analysis_run_id — 轮询 analysisRunId（StartAnalysis 未立即返回时使用）
    step9_query_run_status    — 查询序列运行状态，Finished 表示完成
"""

import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from infrastructure.error_logger import get_error_logger
from network.http_client import HttpClient
from programs.AJL_anjielun.constants import AJLApiConfig, AJLTimeout

logger = get_error_logger()

_LOG = "ChromatographClient"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _datetime_str() -> str:
    """同步样品时间格式：yyyyMMdd HH:mm:ss"""
    return datetime.now().strftime("%Y%m%d %H:%M:%S")


def _place_on_instrument_datetime_str() -> str:
    """step6 放样完成时间格式：yyyyMMddHH:mm:ss"""
    return datetime.now().strftime("%Y%m%d%H:%M:%S")


class ChromatographClient(HttpClient):
    """
    安捷伦 AAC 色谱仪 HTTP 客户端（新版 9 步协议）。

    继承 network.http_client.HttpClient，复用通用 HTTP 通信能力。
    本类只负责组装 AAC 业务接口的请求参数和 Body。

    cdsId / injectorId 可由 step3_query_instrument 动态获取并缓存，
    也可在 AJLApiConfig 中预先填写固定值。

    所有步骤方法返回 (success: bool, message: str)，
    step3 额外返回 cds_id 和 injector_id，
    step7/step8 额外返回 analysis_run_id。
    """

    def __init__(
        self,
        base_url: str = AJLApiConfig.BASE_URL,
        timeout: float = AJLTimeout.API_REQUEST,
    ):
        super().__init__(base_url=base_url, timeout=timeout)
        # 动态获取的仪器标识（step3 填入，优先于 AJLApiConfig 默认值）
        self._cds_id: str = AJLApiConfig.CDS_ID
        self._injector_id: str = AJLApiConfig.INJECTOR_ID
        self._pose: str = AJLApiConfig.VIAL_POSITION
        self._run_id: str = ""

    # ── 步骤 1：登录 ──────────────────────────────────────────────────────────

    def step1_login(
        self,
        username: str = AJLApiConfig.USERNAME,
        password: str = AJLApiConfig.PASSWORD,
    ) -> Tuple[bool, str]:
        """
        步骤 1：登录 AAC 系统，获取访问权限。
        HTTP: PUT /api/v1/Authentication?userName=admin&userPassword=agilent

        返回: (success, message)
        """
        params = {"userName": username, "userPassword": password}
        logger.info(_LOG, f"步骤1 登录 AAC: user={username}")
        result = self.put("/api/v1/Authentication", params=params)
        print("登录结果：", result)
        return result.success, result.message

    # ── 步骤 2：数据同步 ──────────────────────────────────────────────────────

    def step2_sync_samples(
        self,
        vial_barcode: str,
        batch_id: Optional[str] = None,
        vial_count: int = AJLApiConfig.VIAL_COUNT,
    ) -> Tuple[bool, str]:
        """
        步骤 2：将分析任务数据同步给 AAC。
        HTTP: POST /api/v1/Sample/batch

        数据结构：1 个大样品（limsSample） + N 个小样品（vialList）。
        N 由 vial_count 控制，默认值来自 AJLApiConfig.VIAL_COUNT（默认 5）。

        barCode 命名规则：
            limsSample.barCode = vial_barcode          （如 "20240619135729"）
            vialList[i].barCode = vial_barcode-{i+1:03d} （如 "20240619135729-001"）

        参数:
            vial_barcode: 大样品条码（作为 limsSample.barCode，也是小样品条码前缀）
            batch_id:     批次 ID（默认自动生成时间戳）
            vial_count:   小样品数量（默认 AJLApiConfig.VIAL_COUNT = 5）

        返回: (success, message)
        """
        ts = batch_id or _timestamp()
        now_str = _datetime_str()

        vial_list = [
            {
                "type":                   AJLApiConfig.VIAL_TYPE,
                "barCode":                f"{vial_barcode}-{i + 1:03d}",
                "sampleBarCode":          vial_barcode,
                "labName":                AJLApiConfig.VIAL_LAB_NAME,
                "analysisProject":        AJLApiConfig.VIAL_ANALYSIS_PROJECT,
                "analysisMethod":         AJLApiConfig.VIAL_ANALYSIS_METHOD,
                "testCode":               AJLApiConfig.VIAL_TEST_CODE,
                "priority":               AJLApiConfig.VIAL_PRIORITY,
                "addDateTime":            now_str,
                "placeOnInstrumentDateTime": now_str,
                "detReportFlag":          0,
                "limsId1":                "",
                "limsId2":                "",
                "limsId3":                "",
                "desription":             "",
            }
            for i in range(vial_count)
        ]

        payload = {
            "limsSamples": [
                {
                    "barCode":      vial_barcode,
                    "type":         AJLApiConfig.LIMS_SAMPLE_TYPE,
                    "name":         AJLApiConfig.LIMS_SAMPLE_NAME,
                    "equipment":    AJLApiConfig.LIMS_EQUIPMENT,
                    "sampleSite":   AJLApiConfig.LIMS_SAMPLE_SITE,
                    "batchId":      ts,
                    "addDateTime":  now_str,
                    "vialList":     vial_list,
                }
            ]
        }
        print("同步样品数据payload：", payload)
        logger.info(
            _LOG,
            f"步骤2 同步样品数据: sample={vial_barcode} batch={ts} vial_count={vial_count}",
        )
        result = self.post("/api/v1/Sample/batch", json_body=payload)
        return result.success, result.message

    # ── 步骤 3：查询仪器 ──────────────────────────────────────────────────────

    def step3_query_instrument(
        self,
        third_party_identity: str = AJLApiConfig.THIRD_PARTY_IDENTITY,
    ) -> Tuple[bool, str, str, str]:
        """
        步骤 3：通过第三方标识查询目标仪器的 cdsId 及 GCTray 对应的 injectorId。
        HTTP: GET /api/v1/Instrument/auto/3rdPartyIdentity?thirdpartyIdentity=...

        查询成功后自动缓存到 self._cds_id / self._injector_id，
        后续步骤自动使用，无需手动传入。

        返回: (success, message, cds_id, injector_id)
        """
        params = {"thirdpartyIdentity": third_party_identity}
        logger.info(_LOG, f"步骤3 查询仪器: identity={third_party_identity}")
        result = self.get("/api/v1/Instrument/auto/3rdPartyIdentity", params=params)

        if not result.success:
            return False, result.message, "", ""

        data = result.data
        if not data:
            return False, "响应 data 为空，未获取到仪器信息", "", ""

        # ── 解析 cdsId（顶层字段）────────────────────────────────────────────
        # 响应结构：{ "statusCode": 0, "data": { "cdsId": "72", "injectors": [...] } }
        cds_id = str(data.get("cdsId", "")).strip()

        # ── 解析 GCTray 的 injectorId ────────────────────────────────────────
        # 在 data["injectors"] 列表中找 category == "GC_Tray" 的条目
        injector_id = ""
        injectors = data.get("injectors") or []
        for inj in injectors:
            if inj.get("category") == "GC_Tray" and inj.get("id"):
                injector_id = str(inj["id"])
                break

        if not cds_id or not injector_id:
            msg = (
                f"未能从响应中解析 cdsId/injectorId: "
                f"cdsId={cds_id!r}, "
                f"injectors(category/id)="
                f"{[(i.get('category'), i.get('id')) for i in injectors]}"
            )
            logger.error(_LOG, f"步骤3 {msg}")
            return False, msg, "", ""

        # 缓存，后续步骤自动使用
        self._cds_id = cds_id
        self._injector_id = injector_id
        logger.info(_LOG, f"步骤3 仪器信息: cdsId={cds_id} injectorId={injector_id}")
        return True, "success", cds_id, injector_id

    # ── 步骤 4：准备放样 ──────────────────────────────────────────────────────

    def step4_ready_to_place(self) -> Tuple[bool, str, int]:
        """
        步骤 4（建议调用）：确认仪器是否可放样。
        HTTP: PUT /api/v1/Events/ReadyToPlaceVialToInstrument

        statusCode 非 0 含义（由调用方 AJL.py 根据 AJLStep4Policy 决策）：
            -23 仪器暂时不可用  -24 有暂停任务  -25 已有任务运行
            -26 错误锁定        -27 被禁用      -28 模块错误

        返回: (success, message, api_status_code)
            api_status_code — 响应体中的 statusCode 字段，成功时为 0，失败时为负数
        """
        params = {
            "cdsId": self._cds_id,
            "injectorId": self._injector_id,
            "allowPlaceBottlesViaPauseStatus": "false",
        }
        logger.info(_LOG, "步骤4 确认仪器可放样")
        result = self.put("/api/v1/Events/ReadyToPlaceVialToInstrument", params=params)
        api_code = result.raw.get("statusCode", -1) if result.raw else -1
        return result.success, result.message, api_code

    # ── 步骤 5：申请进样器位置 ────────────────────────────────────────────────

    def step5_apply_position(
        self,
        pos: str = AJLApiConfig.VIAL_POSITION,
        wait_seconds: float = AJLTimeout.AFTER_APPLY,
    ) -> Tuple[bool, str]:
        """
        步骤 5：申请仪器进样器位置，AAC 驱动机械动作后返回。
        成功后等待 wait_seconds 秒让机械动作完成。
        HTTP: PUT /api/v1/Events/ApplyInstrumentPositionToPlaceVial

        返回: (success, message)
        """
        params = {
            "cdsId": self._cds_id,
            "injectorId": self._injector_id,
            "pos": self._pose,
            "allowPlaceBottlesViaPauseStatus": "false",
        }
        logger.info(_LOG, f"步骤5 申请进样器位置: pos={pos}")
        result = self.put("/api/v1/Events/ApplyInstrumentPositionToPlaceVial", params=params)
        if result.success and wait_seconds > 0:
            logger.info(_LOG, f"步骤5 等待机械动作完成 ({wait_seconds}s)...")
            time.sleep(wait_seconds)
        return result.success, result.message

    def build_place_vials_list(
        self,
        sample_barcode: str,
        vial_count: int = AJLApiConfig.VIAL_COUNT,
        position_names: Optional[List[str]] = None,
        vial_barcodes: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        构建 step6 放样完成通知的小样品列表。

        1 个大样品（limsSample）对应 vial_count 个小样品（默认 5 个），每项结构：
            positionIndex / positionName / vialBarcode / bottleType / placeOnInstrumentDateTime
        """
        now_str = _place_on_instrument_datetime_str()
        positions = list(position_names or AJLApiConfig.VIAL_POSITIONS)
        if vial_barcodes is None:
            vial_barcodes = [
                f"{sample_barcode}-{i + 1:03d}"
                for i in range(vial_count)
            ]
        if len(positions) < len(vial_barcodes):
            raise ValueError(
                f"放样位置数量不足: positions={len(positions)} vials={len(vial_barcodes)}"
            )
        return [
            {
                "positionIndex": -1,
                "positionName": positions[i],
                "vialBarcode": barcode,
                "bottleType": AJLApiConfig.VIAL_TYPE,
                "placeOnInstrumentDateTime": now_str,
            }
            for i, barcode in enumerate(vial_barcodes)
        ]

    # ── 步骤 6：放样完成 ──────────────────────────────────────────────────────

    def step6_place_complete(
        self,
        place_vials: List[Dict],
        wait_seconds: float = AJLTimeout.AFTER_PLACE,
    ) -> Tuple[bool, str]:
        """
        步骤 6：通知 AAC 放样完成，AAC 驱动进样器归位后返回。
        成功后等待 wait_seconds 秒让机械归位。
        HTTP: PUT /api/v1/Events/PlaceVialsToInstrumentComplete

        参数:
            place_vials: 本次放入仪器的所有小样品列表（通常 5 项）

        返回: (success, message)
        """
        params = {
            "cdsId": self._cds_id,
            "injectorId": self._injector_id,
        }
        payload = place_vials
        barcodes = [item.get("vialBarcode", "") for item in place_vials]
        logger.info(_LOG, f"步骤6 放样完成通知: vial_count={len(place_vials)} barcodes={barcodes}")
        result = self.put(
            "/api/v1/Events/PlaceVialsToInstrumentComplete",
            params=params,
            json_body=payload,
        )
        if result.success and wait_seconds > 0:
            logger.info(_LOG, f"步骤6 等待进样器归位 ({wait_seconds}s)...")
            time.sleep(wait_seconds)
        return result.success, result.message

    # ── 步骤 7：启动分析 ──────────────────────────────────────────────────────

    def step7_start_analysis(self) -> Tuple[bool, str, str]:
        """
        步骤 7：通知 AAC 启动仪器分析自动化流程。
        HTTP: PUT /api/v1/Events/StartAnalysis

        返回: (success, message, analysis_run_id)
            analysis_run_id 可能为空（序列验证延迟），此时用 step8 轮询。
        """
        params = {"cdsId": self._cds_id}
        payload = {
            "overrideByVialBarcodeList": None,
            "overrideByAnalysisTargetList": None,
            "collectorModuleConfig": None,
            "recoveryCollectorModuleConfig": None,
            "analysisRunModel": 2,
            "analysisRunTestVialList": [],
        }
        logger.info(_LOG, "步骤7 启动仪器分析")
        result = self.put("/api/v1/Events/StartAnalysis", params=params, json_body=payload)
        if not result.success:
            return False, result.message, ""

        if isinstance(result.data, dict):
            self._run_id = result.data.get("id", "") or ""
        elif isinstance(result.data, str):
            self._run_id = result.data

        if self._run_id:
            logger.info(_LOG, f"步骤7 AnalysisRunId={self._run_id}")
        else:
            logger.info(_LOG, "步骤7 启动成功，AnalysisRunId 未立即返回，需步骤8轮询")
        return True, result.message, self._run_id

    # ── 步骤 8：轮询 AnalysisRunId ────────────────────────────────────────────

    def step8_poll_analysis_run_id(
        self,
        timeout: float = AJLTimeout.POLL_RUN_ID,
        interval: float = AJLTimeout.POLL_RUN_ID_INTERVAL,
    ) -> Tuple[bool, str, str]:
        """
        步骤 8：轮询 active 接口，直到获取到 AnalysisRunId 或超时。
        HTTP: GET /api/v1/InstrumentAnalysisRun/active

        参数:
            timeout:  最大等待秒数
            interval: 轮询间隔秒数

        返回: (success, message, analysis_run_id)
        """
        params = {"cdsId": self._cds_id}
        deadline = time.time() + timeout
        logger.info(_LOG, f"步骤8 轮询 AnalysisRunId (最多 {timeout}s)...")

        while time.time() < deadline:
            result = self.get("/api/v1/InstrumentAnalysisRun/active", params=params)
            if result.success and isinstance(result.data, dict):
                run_id = result.data.get("id", "")
                if run_id:
                    logger.info(_LOG, f"步骤8 获取到 AnalysisRunId={run_id}")
                    return True, "success", run_id
            time.sleep(interval)

        msg = f"步骤8 轮询 AnalysisRunId 超时（{timeout}s）"
        logger.error(_LOG, msg)
        return False, msg, ""

    # ── 步骤 9：查询序列运行状态 ──────────────────────────────────────────────

    def step9_query_run_status(
        self,
        analysis_run_id: str,
        wait_finished: bool = False,
        timeout: float = AJLTimeout.ANALYSIS_MAX_WAIT,
        interval: float = AJLTimeout.ANALYSIS_POLL_INTERVAL,
    ) -> Tuple[bool, str, str]:
        """
        步骤 9：查询序列运行状态。
        HTTP: GET /api/v1/InstrumentAnalysisRun/{analysis_run_id}

        参数:
            analysis_run_id: 由步骤 7 或 8 获得
            wait_finished:   True 时阻塞轮询直到状态为 Finished 或超时
            timeout:         阻塞轮询最大等待秒数
            interval:        轮询间隔秒数

        返回: (success, message, status)
            status 为 "Finished" 表示可进入下样操作。
        """
        params = {"id": self._run_id}
        path = f"/api/v1/InstrumentAnalysisRun/Id"

        def _query_once() -> Tuple[bool, str, str]:
            result = self.get(path, params=params)
            if not result.success:
                return False, result.message, ""
            status = ""
            if isinstance(result.data, dict):
                status = result.data.get("status", "")
            return True, "success", status

        if not wait_finished:
            return _query_once()

        logger.info(_LOG, f"步骤9 等待分析完成 runId={analysis_run_id} (最多 {timeout}s)...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            ok, msg, status = _query_once()
            if not ok:
                logger.warning(_LOG, f"步骤9 查询失败: {msg}，继续等待...")
            elif status == AJLApiConfig.ANALYSIS_STATUS_FINISHED:
                logger.info(_LOG, f"步骤9 分析完成 runId={analysis_run_id}")
                return True, "success", status
            else:
                logger.info(_LOG, f"步骤9 当前状态: {status}，继续等待...")
            time.sleep(interval)

        msg = f"步骤9 等待分析完成超时（{timeout}s），最后状态未知"
        logger.error(_LOG, msg)
        return False, msg, ""
