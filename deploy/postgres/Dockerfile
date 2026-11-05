FROM postgres:17-alpine@sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b

# 保留官方入口脚本和数据目录权限初始化，用 Alpine 原生工具完成降权。
RUN apk add --no-cache su-exec=0.3-r0 \
    && rm /usr/local/bin/gosu

# 独立删除层记录 whiteout，后续的同名兼容链接不会掩盖底层文件已删除的事实。
RUN ln -s "$(command -v su-exec)" /usr/local/bin/gosu \
    && test "$(gosu postgres id -u)" = "$(id -u postgres)"

COPY su-exec.LICENSE /usr/share/licenses/su-exec/LICENSE
