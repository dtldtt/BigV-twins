# docs/ 目录说明

本目录存放项目的设计文档、测试报告、方案评审和路线图。

## 目录结构

```
docs/
├── README.md              ← 本文件
├── roadmap.md             — 产品路线图（已完成 + 近期 + 中期 + 长期）
├── bug-fix-report.md      — Bug 修复记录
├── proposals/             — 方案设计与评审文档
│   ├── daily-digest-prompt-review.md      — Digest prompt 多方评审（Claude + Qoder）
│   ├── daily-digest-prompt-v2-review.md   — Digest prompt V2 三版本对比
│   └── persona-update-design.md           — Persona 月度更新方案设计
├── test-reports/          — 测试报告
│   ├── prompt-template-test-report.md     — Prompt 模板化集成测试（17/17 PASS）
│   ├── daily-digest-abc-evaluation.md     — Digest 输入方案 A/B/C 评测
│   ├── daily-digest-cross-test-matrix.md  — Digest 新旧 prompt × B/C 交叉矩阵
│   ├── daily-digest-model-comparison.md   — Digest auto/performance/ultimate 模型对比
│   ├── closed-review-prompt-test.md       — 清仓复盘 prompt A/B 版本对比
│   └── closed-review-matrix-test.md       — 清仓复盘 base/enhance × 有无自评 矩阵
└── handoff/               — 项目移交文档（供新 agent/新环境使用）
    ├── project-overview.md
    ├── architecture.md
    ├── user-context.md
    └── session-transcript.jsonl
```

## 用途

- **roadmap.md**：当前进度和未来计划的单一真相来源
- **proposals/**：重大功能的设计方案，经过用户 review 后才实施
- **test-reports/**：prompt 设计的 A/B 测试、交叉矩阵测试的完整记录
- **handoff/**：项目迁移时给新 agent 看的完整上下文
