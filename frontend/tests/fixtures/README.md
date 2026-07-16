# 浏览器 E2E 夹具

本目录只包含项目内合成文本及由这些文本生成的 DOCX，不包含真实客户合同。

1. `nda_high_risk.txt` / `nda_high_risk.docx`：合成 NDA，用于主流程和人工反馈。
2. `service_agreement.txt` / `service_agreement.docx`：合成服务协议，用于验证非 NDA 拒绝。
3. `build_fixtures.py`：使用 Python 标准库确定性重建上述 DOCX。

扫描 PDF 错误流复用 `backend/tests/fixtures/pdf/scanned_image.pdf`，其来源说明见该目录 README。
