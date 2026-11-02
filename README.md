# kiki-text-companion

在你自己的电脑上运行的文本聊天工具：从你和对方的微信私聊记录里学习**你**发消息的习惯，再用云端大模型以你的口吻陪对方文字聊天。

- 只模仿你发出的消息；对方的消息只用作上下文。
- 原始聊天和生成的数据只存在本机；聊天时，相关聊天片段会发给你选择的云端模型服务（默认 DeepSeek，也可填其他 OpenAI 兼容接口）。
- 只自动脱敏手机号、邮箱、证件号等固定格式信息；**人名、地名、学校、单位会原样发送**。两个人都应知情并同意后再使用。
- 不含语音；不保证"像本人"，设置阶段只做免费离线检查。
- 支持 macOS 和 Windows；**Windows 版本未在真实 Windows 电脑上验证**。

## 快速开始

1. 安装 [Python 3.12](https://www.python.org/downloads/)。
2. 用数据线把 iPhone 备份到这台电脑（不要加密本地备份）。
3. 打开 `scripts/share`，双击"开始设置"（Mac 用 `.command`，Windows 用 `.bat`），按提示完成同意确认、选择聊天、填写模型服务和 API key。
4. 双击"开始聊天"。

详细步骤、常见问题、重新设置和删除数据的方法见 [docs/share-text-setup.md](docs/share-text-setup.md)。

## 注意

- 第三方读取微信数据可能违反微信用户协议，请自行判断是否使用。只选择一段双方都同意的私聊，不支持群聊。
- 聊天会产生云端模型费用；程序有本地月预算估算（默认 100 元），请同时在服务商侧设置费用上限。

## 开发

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[share,dev]"
PYTHONPATH=src .venv/bin/python -m pytest -q
```

## 许可证

[PolyForm Noncommercial 1.0.0](LICENSE)：允许个人和非商业用途的使用、修改与再分发，不允许商业用途。
