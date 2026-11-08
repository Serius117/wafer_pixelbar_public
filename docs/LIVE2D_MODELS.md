# Live2D 形象清单与来源

> 下载位置：`olv/upstream/Open-LLM-VTuber/live2d-models/`（OLV 上游目录，gitignored，不入库）
> 控制台入口：控制排「🎭 形象」下拉；`POST /api/live2d-model` → OLV `POST /switch-model`。

| 控制台名称 | 目录 | 来源仓库 | 说明 / 许可 |
|---|---|---|---|
| Hiyori | `live2d-models/Hiyori/` | `cyanfish-x/dsh-live2d-pets`（内置默认，模型 URL 直载） | Live2D 官方示例模型；按 [Live2D 示例模型条款](https://www.live2d.com/eula/live2d-sample-model-terms_cn.html) 使用，保留著作权标注 |
| Mao | `live2d-models/Mao/` | `xkk6663/dsh-live2d-mao` | 仓库代码 MIT；Mao 为 Live2D 官方示例模型，按示例模型条款使用 |
| DeepSeek-chan / 大肥鱼 | `live2d-models/DeepSeek-chan/` | `YJackie-Li/DeepSeek-chan` | 原画/拆分：神宫凉子；Live2D：霓虹猫_NeonNeko。作者免费配布，**禁止重新分发模型文件/二次销售**；仅本机非商业使用 |
| 鲸鱼女仆 | `live2d-models/whale-maid-live2d/` | `daoming07280/whale-maid-svg-layers` | 仓库未声明明确许可证；模型由 psd2live 生成。仅本机使用，不随项目公开仓分发 |

## 实现

- OLV `model_dict.json` 增加四项；`Live2dModel(name)` 读取模型元数据。
- OLV 新增 `POST /switch-model`（同时支持 WS `switch-model`）：更新 default/当前所有 client context，广播 `set-model-and-conf`，不重启 OLV。
- 控制台 `/api/live2d-model` 做白名单校验后代理 OLV；四个模型均实测返回 200，现有 WebSocket 客户端收到对应 `model_info.url`。
- DeepSeek-chan 的原模型文件名「大肥鱼.*」保留在目录内，只把 `.model3.json` 改名为目录名，避免破坏内部相对引用。

## 注意

- `olv/upstream/` 不在版本库内；换机/重建 OLV 上游后需要重新放置这些模型目录并同步 `model_dict.json`。
- 公开仓不包含这四个模型；发布时只保留本清单和接入代码。
