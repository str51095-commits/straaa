# 光伏能源管理平台 ☀️

管理你的光伏电站，按 **日 / 月 / 年** 统计发电量、等效利用小时、收益和减排量。

- 纯 Python 标准库实现（无需 `pip install`），数据存储在本地 SQLite 文件
- 浏览器访问，支持电脑和手机，支持深色模式

## 快速开始

**最简单的方式**：先安装 [Python 3](https://www.python.org/downloads/)（Windows 安装时勾选 “Add python.exe to PATH”），然后
- Windows：双击 `start.bat`
- macOS：双击 `start.command`

浏览器会自动打开平台；启动后的黑色窗口不要关，关掉平台就停止了。

想部署到云服务器、24 小时运行？见 **[DEPLOY.md](DEPLOY.md)**。

命令行方式：

```bash
python3 run.py            # 默认 http://127.0.0.1:8000 ，数据库 solar.db
python3 run.py --port 9000 --db /path/to/my-solar.db
```

打开浏览器后：

1. **电站管理**：添加电站（名称、装机容量 kWp、上网电价、地址、并网日期）
2. **数据录入**：每天录入一次发电量（kWh），或用 CSV 批量导入逆变器/电表导出的数据
3. **概览**：查看今日 / 本月 / 本年 / 累计发电量，以及
   - **日发电量**：选择月份，看当月每天发了多少度电
   - **月发电量**：选择年份，看每月发电量，并与去年同期对比（同比）
   - **年发电量**：历年发电量对比

想先体验？在空白首页点 **生成演示数据**，会创建两个示例电站和约两年的模拟数据。

## 固德威 (GoodWe) 自动同步

对接固德威 **SEMS+** 新平台（默认中国区 `cn-semsplus.goodwe.com`），使用与 SEMS+ App 相同的账号密码。

- **从固德威导入电站**：读取账号下所有电站，自动创建并关联（名称、装机容量、电站 ID）。
- **自动同步**：每 15 分钟写入每座电站的当日发电量。
- **补录历史数据**：读取历史每日发电量（最多 10 年），手动录入的数据不会被覆盖。
- 账号密码错误时自动暂停定时同步，避免账号被锁。

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `GOODWE_ACCOUNT` / `GOODWE_PASSWORD` | SEMS+ 账号、密码（都设置了才会开启） | — |
| `GOODWE_REGION` | 区域：`cn` / `eu` / `hk` / `au` / `us` | `cn` |
| `GOODWE_SYNC_MINUTES` | 同步间隔（分钟） | 15 |
| `GOODWE_ITEM` | 作为发电量的统计项 | `proSystemTotalStats` |
| `GOODWE_API` | 设为 `legacy` 使用旧版 semsportal.com 接口 | `semsplus` |

服务器上运行 `sudo solar-goodwe-check [电站ID]` 可逐步诊断登录、电站列表、发电量统计（不显示密码）。

电站 ID：SEMS+ 网页版电站详情页地址 `#/station_monitor/station_detail?` 后面那段是 Base64 编码，解码后的 `stationId` 即电站 ID；用“从固德威导入电站”则无需手动填写。

对接逻辑在 `solar/semsplus.py`。固德威未公开接口文档，实现参考开源项目 sems-plus-client / goodwe-sems-home-assistant；拿到官方 OpenAPI 后只需替换该文件。

## 登录密码与访问范围

- 未设置密码时，平台只允许本机访问（`127.0.0.1`）。
- 设置环境变量 `SOLAR_PASSWORD` 后会开启登录，并允许局域网/外网访问（例如用手机访问电脑上的平台）。
- `SOLAR_TZ=Asia/Shanghai`：按指定时区计算“今天”（云服务器通常是 UTC 时间，务必设置；安装脚本已自动设置）。

## 统计指标说明

| 指标 | 计算方式 |
|---|---|
| 发电量 | 录入的日发电量求和（1 kWh = 1 度电） |
| 等效利用小时 | 发电量 ÷ 装机容量 (kWp) |
| 收益 | 发电量 × 电站设置的单价（元/kWh） |
| CO₂ 减排 | 发电量 × 0.5703 kg/kWh（全国电网平均排放因子） |
| 节约标准煤 | 发电量 × 0.3 kg/kWh |

选择“全部电站”时，所有电站的发电量、容量、收益会合并统计。

## CSV 导入格式

每行 `日期,发电量kWh`，可带表头，日期支持 `2024-06-01` 或 `2024/6/1`：

```csv
日期,发电量
2024-06-01,210.5
2024-06-02,198.3
```

同一电站同一天重复导入会覆盖原值。“导出 CSV”可将数据导出（Excel 可直接打开）。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/stations` | 电站列表 / 新建 |
| PUT/DELETE | `/api/stations/{id}` | 修改 / 删除（连同其发电记录） |
| GET | `/api/records?station_id=&start=&end=` | 发电记录 |
| POST | `/api/records` | 录入或覆盖 `{station_id, day, kwh, note}` |
| DELETE | `/api/records/{id}` | 删除记录 |
| POST | `/api/import` | `{station_id, csv}` 批量导入 |
| GET | `/api/export?station_id=` | 导出 CSV |
| GET | `/api/stats/summary?station_id=` | 今日/昨日/本月/本年/去年/累计 |
| GET | `/api/stats/daily?month=YYYY-MM` | 某月每日发电量 |
| GET | `/api/stats/monthly?year=YYYY` | 某年每月发电量（含去年同期、同比） |
| GET | `/api/stats/yearly` | 历年发电量 |
| GET | `/api/sync/status` | 固德威同步状态 |
| POST | `/api/sync/run` | 立即同步一次 |

`station_id` 省略时统计全部电站。

## 测试

```bash
python3 -m unittest discover -s tests
```
