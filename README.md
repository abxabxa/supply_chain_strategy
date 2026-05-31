# supply_chain_strategy
五大美股巨头A股供应链 -- 增量抱团策略
每晚21:30自动扫描全A股，跟踪英伟达/特斯拉/苹果/博通/谷歌五大美股巨头的A股供应链标的，按基金持仓季度增量排名，推送Top10策略组合。
核心功能
Table
功能	说明
全动态候选池	零硬编码股票，通过新闻扫描 + 行业关键词从5000+ A股中自动筛选
增量抱团排名	只推送本季机构总持仓环比增长的股票，下降的一律过滤
保守分类	基于 top10_floatholders 真实持仓数据，未知类型默认不计入
外资巨头监控	额外检测高盛/摩根士丹利增持标的并单独推送
Bark推送	iOS推送策略报告 + 新标发现 + 外资增持警报
快速开始
1. 环境变量
bash
Copy
TUSHARE_TOKEN=你的TushareProToken  # 必填，https://tushare.pro/register
BARK_KEY=你的Bark推送密钥         # 可选，不填则不推送
2. 本地运行
bash
Copy
# 安装依赖
pip install -r requirements.txt

# 完整运行
python supply_chain_strategy_merged.py

# 测试Bark推送
python supply_chain_strategy_merged.py --test-push

# 试运行（不推送，只打印报告）
python supply_chain_strategy_merged.py --dry-run
3. GitHub Actions自动运行
项目根目录需包含：
.github/workflows/daily.yml -- 定时触发配置
supply_chain_strategy_merged.py -- 主程序
requirements.txt -- Python依赖
在 GitHub 仓库的 Settings > Secrets and variables > Actions 中添加：
Table
Secret	说明
TUSHARE_TOKEN	Tushare Pro API Token（必填）
BARK_KEY	Bark推送密钥（可选）
定时规则：30 13 * * *（UTC 13:30 = 北京时间 21:30）
数据口径说明
本策略使用 Tushare 的 top10_floatholders 接口获取十大流通股东数据，存在以下固有局限：
只含前十大流通股东，非全部基金持仓。例如某股F10显示全部基金持仓8.73%，但top10中仅4只基金合计5.13%，差额是小基金未进入前十。
holder_type 为中文描述，如"开放式投资基金"、"一般企业"、"自然人"，策略采用子串关键词匹配进行分类。
北上资金识别："香港中央结算有限公司"的holder_type常为"一般企业"，策略通过holder_name关键词特别识别为机构持仓。
保守分类规则
plain
Copy
holder_type 含 "基金"/"ETF"                     → 基金持仓
holder_type 含 "社保"/"QFII"/"保险"/"券商"/"信托"/"银行理财"/"企业年金"/"外资"  → 机构持仓
holder_type 含 "一般法人"/"一般企业"/"个人"/"自然人"                        → 排除
holder_type 未知：
    holder_name 含 "基金"/"ETF"/"联接"        → 基金
    holder_name 含 "社保"/"QFII"/"信托计划"/"养老金"  → 机构
    holder_name 含 "香港中央结算"                → 机构（北上资金）
    其他                                      → 不计入
候选池发现机制
采用双轨并行策略，确保候选池>=10只：
新闻扫描：拉取当日重大新闻，匹配正文中的股票名称
行业关键词过滤：遍历全A股，名称+行业双重匹配（核心关键词高分，纯边缘降权）
合并去重：两条轨道结果合并，取Top15
行业关键词（核心）
光模块、光器件、光芯片、CPO、PCB、AI芯片、GPU、算力、液冷、HBM、高速铜缆、半导体设备、先进封装、汽车电子、智能驾驶、人形机器人、谐波减速器、滚珠丝杠等。
增量抱团策略逻辑
获取本季+上季持仓：对每个候选标的，分别获取当前报告期和上一季度的top10_floatholders
只保留增量>0的标的：总持仓增量 = 基金增量 + 机构增量，必须>0才参与排名
加权评分排序：
基金持仓增量 × 50%
机构持仓增量 × 30%
绝对持仓基数 × 10%
小市值加分 × 10%
拥挤度分组：🟢安全/🟡拥挤/🟠过热/🔴极高，推荐仓位6-12%
额外推送：若候选池中有高盛或摩根士丹利增持的标的，单独推送提醒
报告期推导
基于A股季报披露截止日：
Table
当前月份	可用报告期	说明
1-4月	上年12月31日	待Q1披露
5-8月	本年3月31日	Q1已披露
9-10月	本年6月30日	Q2已披露
11-12月	本年9月30日	Q3已披露
项目文件
plain
Copy
.
├── supply_chain_strategy_merged.py   # 主程序（单文件，824行）
├── requirements.txt                   # Python依赖
├── .github/workflows/daily.yml       # GitHub Actions定时配置
├── .gitignore                        # Git忽略规则
└── README.md                         # 本文件
依赖
plain
Copy
tushare>=1.2.89
requests>=2.28.0
pandas>=2.0.0
python-dotenv>=1.0.0
注意事项
Tushare积分：top10_floatholders需要一定积分权限，请确保账号有足够积分
API频率：内置0.3秒间隔 + 3次重试，一般无需额外控制
candidate_pool.json：候选池缓存文件，记录最新发现的标的，可手动删除重置
运行时长：全量扫描约30-60秒，主要取决于Tushare API响应速度
