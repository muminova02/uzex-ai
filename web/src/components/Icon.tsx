// One consistent outline icon set (24×24, stroke = currentColor). Decorative by default (aria-hidden).
const P: Record<string, string> = {
  home: "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z",
  layers: "m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5",
  sparkles: "M10 3.5 11.6 8a2 2 0 0 0 1.3 1.3L17.5 11l-4.6 1.6a2 2 0 0 0-1.3 1.3L10 18.5l-1.6-4.6a2 2 0 0 0-1.3-1.3L2.5 11l4.6-1.6A2 2 0 0 0 8.4 8zM18 3v4m-2-2h4M19 16v3m-1.5-1.5h3",
  calendar: "M4 6.5A1.5 1.5 0 0 1 5.5 5h13A1.5 1.5 0 0 1 20 6.5v12a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5zM4 10h16M8 3v4m8-4v4",
  filter: "M4 5h16l-6 7.5V18l-4 2v-7.5z",
  search: "M10.5 18a7.5 7.5 0 1 0 0-15 7.5 7.5 0 0 0 0 15Zm5.3-2.2L21 21",
  download: "M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5M5 19h14",
  chevron: "m9 6 6 6-6 6",
  down: "m6 9 6 6 6-6",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-5v-5m0-3h.01",
  handshake: "m11 17 2 2a1.4 1.4 0 0 0 2-2m-4-4 2.5 2.5a1.4 1.4 0 0 0 2-2l-3-3a3 3 0 0 0-4 0l-.9.9a1.4 1.4 0 1 1-2-2L9 6.2a4 4 0 0 1 4.6-.8L15 6h3l3 3v5h-2m-16 0V9l3-3h3M3 14h2l4.5 4.5a1.4 1.4 0 0 0 2-2",
  coins: "M9 7c3.3 0 6-1 6-2.5S12.3 2 9 2 3 3 3 4.5 5.7 7 9 7Zm-6-2.5v5C3 11 5.7 12 9 12m-6-2.5v5C3 16 5.7 17 9 17m-6-2.5v5C3 21 5.7 22 9 22m6-12c3.3 0 6-1 6-2.5M15 7.5v0M9 12c0-1.4 2.7-2.5 6-2.5s6 1.1 6 2.5-2.7 2.5-6 2.5S9 13.4 9 12Zm0 0v5c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5v-5",
  building: "M4 21V5a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v16m0-10h5a1 1 0 0 1 1 1v9M2 21h20M8 8h2m-2 4h2m-2 4h2m8-1h.01M18 18h.01",
  users: "M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20m6-9a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm10 9v-1.5a3.5 3.5 0 0 0-2.6-3.4M15.5 4.1a3.5 3.5 0 0 1 0 6.8",
  send: "M21 3 10 14M21 3l-6.5 18-3.5-7-7-3.5z",
  file: "M14 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7.5zM14 3v4.5h4.5M9 13h6m-6 4h6",
  help: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm-2.6-11.4a2.7 2.7 0 0 1 5.2 1c0 1.8-2.6 2.4-2.6 2.4M12 17h.01",
  menu: "M4 6h16M4 12h16M4 18h16",
  close: "M6 6l12 12M18 6 6 18",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-13v4.5l3 1.5",
  alert: "M12 3 2 20h20zm0 6v5m0 3h.01",
  trend: "m3 16 6-6 4 4 8-8m-5 0h5v5",
  doc: "M8 3h8l4 4v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h3Zm1 9h6m-6 4h6m-6-8h2",
};

export default function Icon({ name, size = "md", label }: { name: keyof typeof P | string; size?: "sm" | "md"; label?: string }) {
  return (
    <svg className={`ic${size === "sm" ? " sm" : ""}`} viewBox="0 0 24 24" aria-hidden={label ? undefined : true}
         role={label ? "img" : undefined} aria-label={label} focusable="false">
      <path d={P[name] ?? ""} />
    </svg>
  );
}
