# 飞书定位批注和复审写回

已用 lark-cli 1.0.88、用户身份，在隔离测试稿验证文字块批注、视频附件块+时间段批注及读回。未向历史达人稿自动留言。

## 命令流程

需要本机已授权的 `lark-cli`；使用现有身份，不索取或复制token。源稿来自本次用户明确指定的稿件/任务，学习扫描不得自动向历史稿追发评论。

```sh
python3 scripts/feishu_review.py snapshot --url '指定飞书文档URL' --out work/review/snapshot.json
python3 scripts/feishu_review.py plan --snapshot work/review/snapshot.json --issues work/review/request.json --out work/review/plan.json
python3 scripts/feishu_review.py apply --plan work/review/plan.json --receipt work/review/receipt.json
```

request包含project=`doubao-app-peiyao`、audience=`internal/client/isolated-test`、campaign、bf、target_version、review_scope（text/image/video数组）、decision（pass/revise/needs_evidence）、coverage、blocking_gaps与issues。完整示例见 `examples/review-request.json`。

coverage的bf_matched/body_read/comments_read/required_links_checked如实填写。视频完整通过另需continuous_viewed、has_audio、audio_listened；图文需all_images_readable。脚本通过不扩成成片通过。零问题pass时issues为空，apply记录no_comments_needed，不向文档发多余评论。

每条意见带block_id、kind、category、problem、change、acceptance、evidence、rule_source。文字quote必须出现在当前有效文本，删除线内容不被当作当前稿。图片/视频带实际token、SHA、下载路径、文件名、版本、方法；视频另有时长、start_seconds/end_seconds。涉及发音/BGM等设requires_audio，未实听则工具拒绝。

prepare只是校验和生成方案，不代替语义审核。apply在每次写前重读全文内容hash/块hash；只忽略飞书因评论增加的comment-refs，真实文案/格式/附件改动会停止。写后核验评论存在且关系绑定目标块。一次一处，不以全文评论伪装定位。

## 重复、失败和并发

- 完整读已解决/未解决评论及回复；已存在同问题先复核，不重复提。脚本的稳定marker负责同方案重试防重，不能替代语义去重。
- 成功API返回不等于成功定位，必须读回锚点。超时先查服务器是否已落单，不盲重写；多条同marker或全篇锚点异常停止本次写入并记录。
- 源稿变化则重新快照和重新审，不自动平移旧句子/时间。不能靠覆盖hash继续写。
- 识别到已有评论但当前版本已变化，复审原问题；旧marker不代表新版本已经审过。
- 同一文档只运行一个写入流程；准备与学习可并行，外部写入串行。原文不覆盖、不删除他人批注、不改客户执行状态。

## 复审回复

先重读原线程全部回复并确认未解决、不是全文评论（接口限制）。在本次授权范围下，向原意见回复“V3当前00:xx…已按该点修改/尚缺…”，引用当前证据；不冒称甲方已放行。

```sh
lark-cli drive +add-reply --url '稿件URL' --comment-id '重新核对的ID' --content @work/review/reply-elements.json --as user
```

JSON文件是元素数组，例如`[{"type":"text","text":"复审结论及当前证据 [APP复审:稳定标记]"}]`。写前查相同标记，写后 +list-replies 全分页读回；超时先查再决定，不直接重发。已解决线程不能回复时本地留闭环结果即可；不自动恢复或关闭甲方线程。没有需处理事项时不为了复审刷回复。

视频实现是**附件块定位评论+文字时间范围**。没有验证播放器原生时间轴锚点，不能声称该能力已实现。
