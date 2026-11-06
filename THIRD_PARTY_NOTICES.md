# 第三方组件

本项目在 `research-desk/app/static/` 下原样附带了以下第三方文件，它们各自遵循原许可证，不属于本项目的 MIT 许可范围：

| 组件 | 位置 | 许可证 |
|---|---|---|
| [PDF.js](https://github.com/mozilla/pdf.js)（`pdf.min.js`、`pdf.worker.min.js`） | `app/static/` | Apache License 2.0 |
| [KaTeX](https://github.com/KaTeX/KaTeX)（含字体） | `app/static/katex/` | MIT |

可选的全文翻译功能调用 [PDFMathTranslate-next](https://github.com/PDFMathTranslate/PDFMathTranslate-next)（AGPL-3.0）。它不在本仓库中，由用户运行「安装全文翻译」脚本自行从 PyPI 安装，作为独立程序被调用。
