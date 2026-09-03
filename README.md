# Research Helper

Research Helper 是一款面向投研分析师的桌面应用，用于将客户需求、市场数据与研究材料整理为可审计的
《场外衍生品投资策略》一页通。系统覆盖需求解析、研究口径确认、自动取数、论点筛选、图表生成、数字溯源、
HTML/PDF 交付，以及可选的 OptionHelper 结构推荐与正式参考报价。

> 本项目用于内部研究与信息整理。生成内容不构成投资建议、销售要约或交易承诺。

## 核心能力

- **需求解析**：识别研究主题、市场范围、事件事实、客户约束和客户点名的 ETF/个股。
- **研究口径确认**：将研究主题、标准行业、主题研究篮子和报价标的明确分离，由分析师审核后冻结。
- **多路径取数**：支持标准行业成分、人工主题篮子和主题 ETF 真实指数成分三种研究路径。
- **证据驱动研究**：结合 iFinD 市场数据、补充材料和论点库形成候选逻辑，并保留原始来源。
- **可审计交付**：逐项校验正文数字，输出一页通 HTML、内部底稿、运行日志和可选 PDF。
- **受控产品流程**：为每只待报价标的生成产品画像，经 OptionHelper 推荐、分析师确认后再正式报价。
- **多标的处理**：支持客户同时指定多只 ETF/个股，逐只形成候选与报价，并由分析师决定哪些写入一页通。

## 工作流程

```text
客户需求与约束
      │
      ▼
需求解析 ──► 市场、研究主题、事件、客户点名标的
      │
      ▼
分析师确认研究口径
      ├─ 标准行业：自动使用数据源行业成分
      ├─ 人工主题篮子：使用分析师勾选的已核验公司
      └─ 主题 ETF：使用 ETF 真实跟踪指数成分
      │
      ▼
市场数据 + sources/ 补充材料 + 事件证据
      │
      ▼
候选论点生成与分析师审核
      │
      ▼
研究观点、图表、数字溯源与一页校验
      │
      ├─► 一页通 HTML / PDF + 内部底稿 + 运行日志
      │
      └─► 可选产品流程
             │
             ▼
        确认待报价标的
             │
             ▼
        Research Helper 逐标的产品画像
             │
             ▼
        OptionHelper 结构推荐
             │
             ▼
        分析师确认结构
             │
             ▼
        OptionHelper 正式定价与报价
             │
             ▼
        分析师选择写入一页通的报价
```

## 关键概念

| 对象 | 作用 | 示例 |
|---|---|---|
| 研究主题 | 回答“研究什么”，用于材料检索、事件分析和报告标题 | 光模块、汽车电子、固态电池 |
| 标准行业 | 数据源能够验证并取得成分股的行业节点 | 通信设备、电力设备、消费电子 |
| 主题研究篮子 | 用于形成主题结论的公司集合 | 中际旭创、新易盛、天孚通信等 |
| 主题 ETF | 以 ETF 真实跟踪指数成分作为研究篮子 | 智能驾驶 ETF 的真实指数成分 |
| 挂钩标的 | 用于产品表达、结构推荐和正式报价的证券 | ETF、指数或客户点名个股 |
| 标的产品画像 | 结构推荐前的标的事实，包括收益、波动、回撤、情景收益和流动性 | 每只待报价标的独立生成 |

研究篮子决定“研究数据从哪里来”，挂钩标的决定“产品对什么报价”。二者可以相关，但不能互相替代。

## 系统要求

- Windows 10/11
- Python 3.10 或更高版本
- 可用的 DeepSeek API Key
- iFinD SDK、账号和密码（主要研究数据源）
- Qt WebEngine/PySide6（桌面界面和 PDF 导出）
- OptionHelper Skill 与独立 Python 环境（仅正式报价需要）

## 安装

在项目根目录创建并启用 Python 环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install requests PySide6 matplotlib seaborn pyecharts akshare pypdf python-docx
```

iFinD 的 `iFinDPy` 由同花顺终端或官方 SDK 安装程序提供，不建议通过非官方 PyPI 包替代。安装完成后可运行：

```powershell
python -c "import iFinDPy; print(iFinDPy.__file__)"
```

## 配置

在项目根目录创建 `config.local.json`。该文件已加入 `.gitignore`，不得提交到版本库。

```json
{
  "DEEPSEEK_API_KEY": "your-api-key",
  "DEEPSEEK_MODEL": "deepseek-v4-flash",
  "IFIND_ACCOUNT": "your-ifind-account",
  "IFIND_PASSWORD": "your-ifind-password",
  "OPTIONHELPER_SKILL_ROOT": "C:/path/to/option-helper",
  "OPTIONHELPER_PYTHON": "C:/path/to/optionhelper/python.exe",
  "OPTIONHELPER_DEFAULT_CONSTRAINTS": {
    "horizon": "3个月",
    "max_loss": "100%",
    "principal_fluctuation": true
  }
}
```

同名环境变量优先于 `config.local.json`。主流程常用变量如下：

| 配置项 | 必需 | 用途 |
|---|---:|---|
| `DEEPSEEK_API_KEY` | 是 | 需求解析、论点规划和研究写作 |
| `DEEPSEEK_MODEL` | 否 | 覆盖默认模型 |
| `DEEPSEEK_BASE_URL` | 否 | 使用兼容 API 地址 |
| `IFIND_ACCOUNT` / `IFIND_PASSWORD` | 建议 | Research Helper 研究取数 |
| `RESEARCH_HELPER_GUI_PYTHON` | 否 | 指定安装了 PySide6 的桌面端解释器 |
| `OPTIONHELPER_SKILL_ROOT` | 报价时 | OptionHelper Skill 根目录 |
| `OPTIONHELPER_PYTHON` | 报价时 | OptionHelper 独立解释器 |
| `OPTIONHELPER_SELECTION_PATH` | 否 | 一次性待报价 selection 路径 |

OptionHelper 使用的 iFinD Refresh Token 由其就绪检查流程保存至本地 `.optionhelper/`，不写入
`config.local.json`，也不与 Research Helper 的 iFinD 账号密码混用。

## 快速开始

### 桌面应用

推荐使用桌面界面完成完整工作流：

```powershell
python gui/start.py
```

桌面端提供客户需求与约束输入、补充材料上传/粘贴、研究口径确认、主题篮子勾选、事件证据管理、候选逻辑审核、
运行进度、交付预览、历史运行以及正式报价队列。

### 命令行

生成一份研究报告：

```powershell
python main.py -b "近期黄金价格波动加大，客户想了解黄金 ETF 的投资机会。"
```

人工选择正文主轴并导出 PDF：

```powershell
python main.py -b "光模块需求上修，分析相关投资机会。" --pick --pdf
```

指定客户约束：

```powershell
python main.py -b "分析消费电子板块的投资机会。" `
  --horizon 6个月 `
  --max-loss 20% `
  --principal-fluctuation yes `
  --return-preference "更偏上涨参与"
```

需要人工确认跨市场或研究口径时：

```powershell
python main.py -b "分析港股互联网板块的投资机会。" --confirm-market
```

扫描市场并生成候选主题：

```powershell
python main.py
```

根据扫描结果生成指定主题：

```powershell
python main.py 1 4
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `-b`, `--brief` | 输入客户需求 |
| `--pick` | 由分析师选择正文候选论点 |
| `--confirm-market` | 启用市场与研究口径确认 |
| `--overrides <json>` | 导入人工数据补充文件 |
| `--pdf` | 导出 PDF 并执行真实页数校验 |
| `--optionhelper recommend` | 仅运行结构推荐，不正式报价 |
| `--optionhelper quote` | 使用已确认的一次性 selection 正式报价 |
| `--horizon` | 客户期限 |
| `--max-loss` | 最大损失比例，范围 0%–100% |
| `--principal-fluctuation yes\|no` | 是否接受本金波动 |
| `--return-preference` | 客户收益偏好原话 |

## 研究取数路径

确认页中的“研究取数路径”提供三个互斥选项：

1. **标准行业**：系统使用数据源认可的行业节点及其成分股。无需人工勾选公司，也不要求填写 ETF。
2. **人工主题篮子**：系统只使用分析师勾选并已核验的公司。适合光模块、汽车电子等细分或跨行业主题。
3. **主题 ETF**：系统读取 ETF 的真实跟踪指数成分。ETF 成分无法核验时不会回退到宽行业或单只代表股。

在标准行业和人工主题篮子路径中，所选 ETF 仅用于后续产品报价，不会覆盖研究篮子。客户同时点名 ETF 和个股时，
个股进入待确认主题篮子，ETF 保留为主题取数候选或报价工具。

## 补充材料与人工数据

- `sources/`：存放当次研究使用的 PDF、TXT、Markdown 或 DOCX 材料。
- 桌面端“补充材料”支持上传文件，也支持粘贴正文并单独填写来源。
- 事件型需求可维护“事件事实”和“传导关系”；缺少关键传导证据时系统会要求补充，而不会直接分析宽行业。
- `--overrides` 仅用于补充自动数据源无法取得的展示或解释字段；人工值不能触发机械论点。

材料内容、客户提供事实和系统推断会分开标记。无法核验的客户事实可以作为“客户提供”保留，但不得伪装成公开数据。

## 产品推荐与正式报价

研究完成后，产品流程独立运行，不会重新解析需求或重跑研究：

1. 分析师确认待报价 ETF/个股；
2. Research Helper 为每只标的生成产品画像；
3. OptionHelper 根据共同研究观点、客户约束和当前标的画像生成结构候选；
4. 分析师确认采用的候选结构；
5. OptionHelper 自行取得现价、定价波动率、利率、分红率和交易日历并生成正式报价；
6. 多份报价完成后，由分析师勾选写入一页通的项目。

产品画像包括近 20/60 日收益、20 日实现波动率及历史分位、近一年最大回撤、历史情景收益带和流动性。
这些数据用于推荐前理解标的，不替代 OptionHelper 的正式定价参数。

## 输出文件

所有运行结果写入 `output/`：

| 文件 | 用途 |
|---|---|
| `onepager_<主题>.html` | 一页通 HTML；屏幕查看时支持本地交互图 |
| `onepager_<主题>.pdf` | 正式 PDF；仅实测一页时通过交付校验 |
| `onepager_<主题>_内部底稿.md` | 研究口径、数据缺口、证据出处和产品调用记录 |
| `*_内部交互复核.html` | 主题篮子、历史序列和 ETF 候选的内部交互复核页 |
| `output/runs/<run_id>.json` | 运行摘要、阶段状态、产物和恢复建议 |
| `output/runs/<run_id>.jsonl` | 逐事件运行日志 |
| `*.optionhelper-handoff.json` | 冻结的 OptionHelper 研究观点包 |
| `*.product-profile-<代码>.json` | 每只待报价标的的产品画像 |

`output/`、`sources/`、`references/`、本地缓存和凭证均被排除在版本控制之外。

## 图表与交付质量

- 时间序列使用折线图或面积图；公司横向比较使用排序条形图；两个可比变量使用散点图。
- 不同量纲、不同业务含义的单点指标不会被拼成“趋势图”，必要时降级为数据卡。
- 每张图保留数据截至日、单位、样本口径和结论标题。
- HTML 使用本地 ECharts 交互层；PDF/打印使用同一数据生成的静态图。
- 正文数字必须能够回溯到数据字段或材料出处；无法回溯的数字会进入校验问题。
- PDF 只有在真实渲染结果为一页时才通过正式交付校验。

## 项目结构

```text
main.py                     命令行入口与端到端编排
gui/                        PySide6 桌面应用
core/                       需求解析、取数、研究、校验和 OptionHelper 桥接
llm/                        LLM 客户端
render/                     图表、HTML、PDF 与内部底稿
tests/                      自动化测试
THESIS_LIBRARY.md           机器可读论点库
underlying_map.json         行业/主题与常用工具映射
sources/                    当次补充材料工作区
references/                 内部版式参考，不参与自动研究
output/                     生成结果和运行日志
data_cache/                 本地数据缓存
```

## 开发与测试

运行完整测试：

```powershell
python -m unittest discover -s tests -v
```

提交前建议同时执行：

```powershell
python -m compileall -q main.py gui core render
git diff --check
```

新增能力时应同步更新测试和相关项目文档。历史修改记录不在 README 中重复维护。

## 常见问题

### DeepSeek 网页可用，但 API 无法访问

网页会话和 `https://api.deepseek.com` 是不同链路。请确认 API Key、`DEEPSEEK_BASE_URL`、VPN/代理策略和 443 端口。
Research Helper 的部分国内数据源会使用直连模式，系统代理可用不代表所有 API 都可达。

### iFinD 取数失败

确认 iFinD 终端/SDK 已安装、`iFinDPy` 可导入、账号密码有效，并检查周度额度。系统会缓存静态或慢变数据；
关键数据缺失时不会以代表个股或宽行业静默替代。

### 研究完成但没有正式报价

研究完成与报价完成是两个状态。请检查是否已确认待报价标的、OptionHelper 是否通过就绪检查、是否存在未消费的
`selection.pending.json`，以及报价任务区显示的具体失败阶段。

### HTML 正常但 PDF 未通过

正式 PDF 需要 PySide6/Qt WebEngine，并且必须实测为一页。报价写回后系统会重新生成并再次校验，不会沿用报价前 PDF。

### HTML 交互图未显示

确认 `assets/vendor/echarts.min.js` 与报告保持在项目目录关系中。即使交互层不可用，PDF 和打印仍应保留静态图。

## 安全与合规

- 不要提交 `config.local.json`、`.env`、`.optionhelper/`、`data/`、`result/`、客户材料或生成报告。
- 日志不得记录 API Key、密码、Refresh Token、请求头或完整外部敏感响应。
- 客户材料、第三方研报和正式报价文件仅应保存在授权目录，并遵循机构的数据保留政策。
- 分发桌面程序时不要内置个人凭证；多人使用应通过公司受控后端统一鉴权、限流和审计。

## 相关文档

- [THESIS_LIBRARY.md](THESIS_LIBRARY.md)：论点库与机器判定规则
