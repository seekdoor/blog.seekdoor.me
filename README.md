# blog.seekdoor.me

Typecho 到 Hugo + FixIt 的可重复迁移项目。

## 本地生成

~~~powershell
py scripts/migrate_site.py --database .\blog_20260726_084413.db --files .\blog_data_20260726_084413 --output .
hugo mod get github.com/hugo-fixit/FixIt@v0.4.5
hugo --gc --minify
hugo server --disableFastRender
~~~

完整迁移入口只读取 Typecho SQLite 和文件备份。它生成公开 Hugo 内容、公开上传文件、Vercel 重定向和不含敏感信息的迁移报告；无法恢复的旧媒体会生成明确标注的回退图。用于 Waline 的完整历史评论导出写入 reports/waline-comments.private.json，该文件已被 Git 忽略。

## Vercel

将本仓库创建为 GitHub 仓库后，在 Vercel 建立两个项目：

1. 主站根目录为仓库根目录，构建命令为 hugo --gc --minify，输出目录为 public。
2. Waline 根目录为 waline/，使用其中的 vercel.json。

主项目绑定 blog.seekdoor.me，Waline 项目绑定 comment.blog.seekdoor.me。主项目使用 Hugo Extended 0.161.1；Waline 项目使用 Node.js 22。

## Waline / MongoDB Atlas

复制 waline/.env.example 的变量到 Waline Vercel 项目。不要把真实环境变量、SQLite 备份或 reports/waline-comments.private.json 提交到 Git。

在 Waline 已部署并连接到 Atlas 后，在受控本机执行：

~~~powershell
node .\waline\scripts\import-typecho-comments.mjs --input .\reports\waline-comments.private.json --mongo-uri $env:MONGODB_URI --database waline
~~~

先加 --dry-run 检查 56 条评论和 13 条回复，再进行真实导入。导入器以 typechoCoid 幂等写入，不会在重试时重复创建评论。
