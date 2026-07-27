# PDF 边界夹具

本目录文件均由项目内合成，不包含真实客户合同或未经授权的合同文本。

1. `scanned_image.pdf`：只有合成位图、没有文本层，用于验证 Docling 按需 OCR、页码和坐标映射。
2. `empty_text.pdf`：空白 PDF，用于验证无有效文本。
3. `encrypted.pdf`：密码保护的合成 PDF，用于验证加密错误；测试密码为 `fixture-password`，解析器不会使用该密码解密。
4. `complex_font.pdf`：使用无 Unicode 映射 CID 字体的最小合成 PDF，用于验证复杂字体映射失败。
5. `damaged.pdf`：故意截断的最小合成数据，用于验证损坏文件错误。

这些夹具只验证失败分类和流程边界，不应作为可审查合同输入。
