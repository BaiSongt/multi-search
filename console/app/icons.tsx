const paths = {
  overview: "M4 4h6v6H4z M14 4h6v6h-6z M4 14h6v6H4z M14 14h6v6h-6z",
  providers: "M12 3 3 8l9 5 9-5-9-5Z M3 12l9 5 9-5 M3 16l9 5 9-5",
  requests: "M3 12h4l3-7 4 14 3-7h4",
  jobs: "M8 5h12 M8 12h12 M8 19h12 M4 5h.01 M4 12h.01 M4 19h.01",
  routing: "M6 4v16 M12 4v16 M18 4v16 M4 8h4 M10 16h4 M16 10h4",
  images: "M4 4h16v16H4z M4 16l5-5 5 5 3-3 3 3 M15 8h.01",
  tenants: "M4 20V7l8-3v16 M12 10h8v10 M2 20h20 M8 9h.01 M8 13h.01 M8 17h.01 M16 14h.01 M16 17h.01",
  quotas: "M5 20v-5 M12 20V4 M19 20v-9 M3 20h18",
  audit: "M12 3 4 6v5c0 5 3 8 8 10 5-2 8-5 8-10V6l-8-3Z M9 12l2 2 4-4",
  cache: "M20 6c0 2-3.6 3-8 3S4 8 4 6s3.6-3 8-3 8 1 8 3Z M4 6v12c0 2 3.6 3 8 3s8-1 8-3V6 M4 12c0 2 3.6 3 8 3s8-1 8-3",
  settings: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z M12 2v3 M12 19v3 M2 12h3 M19 12h3 M5 5l2 2 M17 17l2 2 M5 19l2-2 M17 7l2-2",
  keys: "M14 4a5 5 0 0 0-4.5 7.2L3 18v3h3l2-2v-2h2l2-2a5 5 0 1 0 2-11Z M16 8h.01",
  search: "M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0Z M14.5 14.5 20 20",
  docs: "M5 3h10l4 4v14H5z M14 3v5h5 M8 12h8 M8 16h8",
  team: "M16 20v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M13 7a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z M16 4a3 3 0 0 1 0 6 M20 20v-2a4 4 0 0 0-3-3.9",
  user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  swap: "M3 8h17 M16 4l4 4-4 4 M21 16H4 M8 12l-4 4 4 4",
  theme: "M12 3a9 9 0 1 0 0 18V3Z M12 3a9 9 0 0 1 0 18",
  mail: "M3 5h18v14H3z M3 6l9 7 9-7",
  lock: "M5 10h14v11H5z M8 10V7a4 4 0 1 1 8 0v3 M12 14v3",
  arrow: "M4 12h15 M13 6l6 6-6 6",
  refresh: "M20 7a9 9 0 1 0 1 7 M20 3v5h-5",
  code: "M8 7l-5 5 5 5 M16 7l5 5-5 5 M14 4l-4 16",
  admin: "M4 5h16v12H4z M8 21h8 M12 17v4 M7 11h3l2-3 2 5 2-2h1",
  menu: "M4 6h16 M4 12h16 M4 18h16",
  chevron: "m6 9 6 6 6-6",
} as const;

export function Icon({ name }: { name: keyof typeof paths }) {
  return (
    <svg className="icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round">
      <path d={paths[name]} />
    </svg>
  );
}

export function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect width="32" height="32" rx="8" fill="#2358db" />
      <path d="M6.5 20V12l4 5 4-5v8 M19 12a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z M22 19l3.5 3.5" fill="none" stroke="#fff" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="25" cy="8" r="1.5" fill="#9ce9ff" />
    </svg>
  );
}
