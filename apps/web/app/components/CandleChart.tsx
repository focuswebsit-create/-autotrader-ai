"use client";

import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getBackend } from "@/lib/client";
import { fmtInt, fmtNum, fmtUtc } from "@/lib/format";
import { explain } from "@/lib/i18n";
import type { CandleDto, CandlesResponse, Interval } from "@/lib/types";

type Preset = "1D" | "1W" | "1M" | "3M" | "ALL" | "CUSTOM";
const PRESETS: { id: Preset; label: string; days: number | null }[] = [
  { id: "1D", label: "יום", days: 1 },
  { id: "1W", label: "שבוע", days: 7 },
  { id: "1M", label: "חודש", days: 30 },
  { id: "3M", label: "3 חודשים", days: 90 },
  { id: "ALL", label: "הכל", days: null },
  { id: "CUSTOM", label: "טווח…", days: null },
];
const INTERVAL_S: Record<Interval, number> = { "30m": 1800, "1h": 3600 };
const POLL_MS = 30_000;

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function palette() {
  return {
    bg: cssVar("--card"),
    fg: cssVar("--fg"),
    muted: cssVar("--muted"),
    grid: cssVar("--grid"),
    up: cssVar("--up"),
    down: cssVar("--down"),
    forming: cssVar("--forming"),
    warn: cssVar("--warn"),
  };
}

function todayUtc(offsetDays = 0): string {
  return new Date(Date.now() + offsetDays * 86_400_000).toISOString().slice(0, 10);
}

type Legend = { c: CandleDto; forming: boolean } | null;

export default function CandleChart() {
  const [interval, setInterval_] = useState<Interval>("30m");
  const [preset, setPreset] = useState<Preset>("1W");
  const [from, setFrom] = useState(todayUtc(-14));
  const [to, setTo] = useState(todayUtc());
  const [data, setData] = useState<CandlesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const [legend, setLegend] = useState<Legend>(null);

  const container = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const byTime = useRef<Map<number, { c: CandleDto; forming: boolean }>>(new Map());

  const key = `${interval}|${preset}|${from}|${to}`;
  const customInvalid = preset === "CUSTOM" && from > to;
  const loading = !customInvalid && loadedKey !== key;

  const load = useCallback(async () => {
    if (customInvalid) return;
    // Relative ranges are computed at fetch time, so "last week" stays current.
    const params: Record<string, string> = { interval, limit: "5000" };
    if (preset === "CUSTOM") {
      params.start = `${from}T00:00:00Z`;
      params.end = new Date(new Date(`${to}T00:00:00Z`).getTime() + 86_400_000).toISOString();
    } else {
      const days = PRESETS.find((p) => p.id === preset)?.days;
      if (days) params.start = new Date(Date.now() - days * 86_400_000).toISOString();
    }
    const res = await getBackend<CandlesResponse>("candles", params);
    if (res.ok) {
      setData(res.data);
      setError(null);
    } else {
      setError(res.error);
    }
    setLoadedKey(key);
  }, [interval, preset, from, to, key, customInvalid]);

  // Fetch on change; keep polling while the range is open-ended ("until now").
  useEffect(() => {
    const first = window.setTimeout(() => void load(), 0);
    const id = preset === "CUSTOM" ? undefined : window.setInterval(() => void load(), POLL_MS);
    return () => {
      window.clearTimeout(first);
      if (id) window.clearInterval(id);
    };
  }, [load, preset]);

  // Create the chart once.
  useEffect(() => {
    if (!container.current) return;
    const p = palette();
    const chart = createChart(container.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: p.bg },
        textColor: p.muted,
        fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif",
        attributionLogo: true,
      },
      grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
      rightPriceScale: { borderColor: p.grid },
      timeScale: { borderColor: p.grid, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
      localization: {
        locale: "en-US",
        timeFormatter: (t: Time) => `${fmtUtc(new Date((t as number) * 1000).toISOString())} UTC`,
      },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: p.up,
      downColor: p.down,
      borderVisible: false,
      wickUpColor: p.up,
      wickDownColor: p.down,
      priceLineVisible: true,
    });
    const volume = chart.addSeries(HistogramSeries, {
      priceScaleId: "vol",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.25 } });

    const onMove = (param: MouseEventParams<Time>) => {
      const t = param.time as number | undefined;
      const hit = t != null ? byTime.current.get(t) : undefined;
      setLegend(hit ?? null);
    };
    chart.subscribeCrosshairMove(onMove);
    chartRef.current = chart;
    candleRef.current = candles;
    volRef.current = volume;
    markersRef.current = createSeriesMarkers(candles, []);

    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const retheme = () => {
      const q = palette();
      chart.applyOptions({
        layout: { background: { type: ColorType.Solid, color: q.bg }, textColor: q.muted },
        grid: { vertLines: { color: q.grid }, horzLines: { color: q.grid } },
      });
      candles.applyOptions({ upColor: q.up, downColor: q.down, wickUpColor: q.up, wickDownColor: q.down });
    };
    media.addEventListener("change", retheme);
    return () => {
      media.removeEventListener("change", retheme);
      chart.unsubscribeCrosshairMove(onMove);
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  const gaps = useMemo(() => {
    if (!data) return [];
    const step = INTERVAL_S[data.interval];
    const out: { t: number; missing: number }[] = [];
    for (let i = 1; i < data.candles.length; i++) {
      const diff = data.candles[i].t - data.candles[i - 1].t;
      if (diff > step) out.push({ t: data.candles[i].t, missing: diff / step - 1 });
    }
    return out;
  }, [data]);

  // Push data into the chart.
  useEffect(() => {
    const candles = candleRef.current;
    const volume = volRef.current;
    if (!candles || !volume || !data) return;
    const p = palette();
    const map = new Map<number, { c: CandleDto; forming: boolean }>();
    const cs: CandlestickData<Time>[] = [];
    const vs: HistogramData<Time>[] = [];
    const push = (c: CandleDto, forming: boolean) => {
      map.set(c.t, { c, forming });
      const up = Number(c.c) >= Number(c.o);
      const item: CandlestickData<Time> = {
        time: c.t as UTCTimestamp,
        open: Number(c.o),
        high: Number(c.h),
        low: Number(c.l),
        close: Number(c.c),
      };
      if (forming) {
        item.color = p.forming;
        item.wickColor = p.forming;
        item.borderColor = p.forming;
      }
      cs.push(item);
      vs.push({
        time: c.t as UTCTimestamp,
        value: Number(c.v),
        color: forming ? p.forming : `${up ? p.up : p.down}66`,
      });
    };
    data.candles.forEach((c) => push(c, false));
    if (data.forming && (!data.candles.length || data.forming.t > data.candles[data.candles.length - 1].t)) {
      push(data.forming, true);
    }
    byTime.current = map;
    candles.setData(cs);
    volume.setData(vs);
    const markers: SeriesMarker<Time>[] = gaps.map((g) => ({
      time: g.t as UTCTimestamp,
      position: "aboveBar",
      color: p.warn,
      shape: "arrowDown",
      text: `פער: ${g.missing}`,
    }));
    markersRef.current?.setMarkers(markers);
    chartRef.current?.timeScale().fitContent();
  }, [data, gaps]);

  const last = useMemo(() => {
    if (!data) return null;
    if (data.forming) return { c: data.forming, forming: true };
    const c = data.candles[data.candles.length - 1];
    return c ? { c, forming: false } : null;
  }, [data]);
  const shown = legend ?? last;

  return (
    <section className="card" aria-labelledby="chart-title">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 id="chart-title" className="text-base font-semibold">
          גרף נרות · <bdi dir="ltr">{data?.coin ?? "xyz:CL"}</bdi>
        </h2>
        <div className="seg" role="group" aria-label="טווח זמן לנר">
          {(["30m", "1h"] as Interval[]).map((iv) => (
            <button key={iv} type="button" aria-pressed={interval === iv} onClick={() => setInterval_(iv)}>
              <bdi dir="ltr">{iv}</bdi>
            </button>
          ))}
        </div>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="seg seg-wrap" role="group" aria-label="טווח תאריכים">
          {PRESETS.map((p) => (
            <button key={p.id} type="button" aria-pressed={preset === p.id} onClick={() => setPreset(p.id)}>
              {p.label}
            </button>
          ))}
        </div>
        {preset === "CUSTOM" && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <label className="flex items-center gap-1">
              מ־
              <input type="date" dir="ltr" value={from} max={to} onChange={(e) => setFrom(e.target.value)} className="input" />
            </label>
            <label className="flex items-center gap-1">
              עד
              <input type="date" dir="ltr" value={to} min={from} onChange={(e) => setTo(e.target.value)} className="input" />
            </label>
            <span className="text-xs muted">(UTC)</span>
          </div>
        )}
      </div>

      <dl className="ohlc mb-2" aria-live="polite">
        {shown ? (
          <>
            <div><dt>זמן</dt><dd dir="ltr">{fmtUtc(shown.c.open_time)}</dd></div>
            <div><dt>O</dt><dd dir="ltr">{fmtNum(shown.c.o)}</dd></div>
            <div><dt>H</dt><dd dir="ltr">{fmtNum(shown.c.h)}</dd></div>
            <div><dt>L</dt><dd dir="ltr">{fmtNum(shown.c.l)}</dd></div>
            <div><dt>C</dt><dd dir="ltr">{fmtNum(shown.c.c)}</dd></div>
            <div><dt>Vol</dt><dd dir="ltr">{fmtNum(shown.c.v, 2)}</dd></div>
            <div>
              <dt>מצב</dt>
              <dd>{shown.forming ? <span className="badge badge-forming">נר פתוח · לא סגור</span> : <span className="badge badge-ok">סגור</span>}</dd>
            </div>
          </>
        ) : (
          <div className="muted text-sm">{loading ? "טוען…" : "אין נתונים"}</div>
        )}
      </dl>

      <div dir="ltr" className="chart-box" ref={container} role="img" aria-label={`גרף נרות ${interval}`} />

      <div className="mt-2 flex flex-wrap justify-between gap-2 text-xs muted">
        <span>
          {data ? (
            <>
              {fmtInt(data.count)} נרות סגורים
              {data.forming ? " + נר פתוח (אפור)" : ""} · כל הזמנים ב־UTC
              {data.truncated ? " · מוצגים 5,000 הנרות האחרונים בטווח" : ""}
            </>
          ) : null}
        </span>
        <span>{gaps.length > 0 ? <span style={{ color: "var(--warn)" }}>{gaps.length} פערים בטווח המוצג</span> : data ? "אין פערים בטווח המוצג" : ""}</span>
      </div>
      {customInvalid && <p className="mt-2 text-sm err">{explain("START_AFTER_END")}</p>}
      {error && <p className="mt-2 text-sm err">{explain(error)}</p>}
    </section>
  );
}
