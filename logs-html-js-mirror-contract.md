# logs.html JS 镜像契约

状态：已消除。

日志行 HTML 的唯一运行时渲染入口是 `app/templates/_log_rows.html`：

- 首屏 `app/templates/logs.html` 通过 `{% include "_log_rows.html" %}` 渲染。
- `/admin/api/logs` 返回同一 partial 渲染得到的 `rows_html`。
- 无限滚动直接插入 `rows_html`，不再维护 JS 侧日志行模板。
