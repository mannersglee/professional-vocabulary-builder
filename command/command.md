我要搭建一个专业词汇积累和学习的软件平台

输入语料包括：文件（docx，PDF，...），网页链接等

工作流程：
1. 训练一个本地的模型用于将语料转化为 TXT 文件
2. 接入大模型 AI，将 TXT 文件发送给 AI，得到一份结构化的单词本
    - 需要专业的 SKILL.md，用于规范大模型的输出
    - 需要支持不同的大模型
3. 可以更新、维护这份结构化单词本，即：每当添加新的语料时，对已有单词本更新
    - 每次运行新的语料都会生成对应的结构化单词本（可以设置定时清理或者永久保存）
    - 总结构化单词本是所有单词本的并集
4. 搭建一个基于单词本的英语学习平台（使用 python）
    - 用户可以选择单词本进行筛选（如语料来源，添加日期，专业方向等）然后进行学习、复习
    - 新添加或新更新的单词有更高的权重
    - 模仿不背单词、百词斩等平台
5. 搭建单词联想学习平台，即：可以查看单词的衍生、近义、常见组合等单词，搭建网状联想知识网络

## 下面是结构化单词本的参考字段（不限于这些字段）：

字段名	含义	更新规则
term_id	术语唯一 ID	创建后不变
english_term	英文标准术语	保留规范形式
chinese_translation	中文专业译名	可校正
phonetic_symbols    音标    
part_of_speech	词性	必要时补充
english_definition	英文专业定义	可完善
chinese_definition	中文专业解释	可完善
domain	专业领域	可补充
subdomain	细分领域	可补充
category	术语类型	可调整
difficulty	L1–L4 难度	可调整
priority	P1–P3 学习优先级	可调整
example_sentence	英文例句	可补充
source_sentence     原文例句    
collocations	常见搭配	合并去重
related_terms	关联术语	合并去重
abbreviation	缩写	可补充
sources	文献来源	追加去重
source_locations	页码、章节等	追加去重
original_contexts	原文上下文	追加去重
frequency	已处理语料中的累计出现次数	按新语料累加
learning_status	New / Learning / Mastered	保留用户记录
last_reviewed	上次复习日期	保留
next_review	下次复习日期	保留
created_at	首次入库日期	创建后不变
updated_at	最近内容更新日期	内容变更时更新

## Demo
我想先开发一个 DEMO：
- 编写成熟的，稳健的 SKILL，先不用实现大模型AI的自动使用，我想手动将 标准化语料的TXT文件、SKILL发送给AI，得到目标结构化单词本
- 学习和联想网络平台使用python开发，运行时可唤起网页
- 保留单词阅读功能接口（暂且不用实现）
- 我在工作路径下保存了一个PDF文件，用于测试：`7 brief_review_of_deep_learning_methods_in_mortality_forecasting.pdf`
- python 解释路径为：`/opt/anaconda3/envs/myenv`