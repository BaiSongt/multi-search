type Cell = string | number | boolean | null | undefined;

function csvCell(value: Cell): string {
  let text = value == null ? "" : String(value);
  // 防止包含 API 可控文本的 CSV 单元格被电子表格当作公式执行。
  if (typeof value === "string" && /^(?:\s*[=+\-@]|[\t\r\n])/.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

export function downloadCsv(name: string, headers: string[], rows: Cell[][]) {
  const csv = [headers, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
  const url = URL.createObjectURL(new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}
