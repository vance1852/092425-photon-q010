"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import PhotonService


def run() -> dict:
    service = PhotonService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "photon-admin")
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(token, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    result = service.analyze(token, "LOT-DEMO")
    # 工程师提交的光功率单位为微瓦：500 uW = 0.5 mW，响应度必须按毫瓦换算。
    responsivity = service.compute_responsivity(token, 0.4, 500, "ma", "uw")
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")
    return {
        "status": "ok",
        "lot": result["lot_id"],
        "peak": result["spectrum"]["peak_wavelength_nm"],
        "events": len(service.audit(token, "LOT-DEMO")),
        "responsivity_ma_per_mw": responsivity["responsivity"],
        "power_unit": responsivity["power_unit"],
        "conversion_version": responsivity["conversion_version"],
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
