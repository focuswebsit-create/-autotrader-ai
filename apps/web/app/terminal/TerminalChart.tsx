"use client";

import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getBackend } from "@/lib/client";
import { ageFrom, fmtAge, fmtNum, fmtUtc } from "@/lib/format";
import { ema, rsi } from "@/lib/indicators";
import type { Position, Trade } from "@/lib/paper";
import type { CandlesResponse, Interval } from "@/lib/types";

const STEP: Record<Interval, number> = { "30m": 1800, "1h": 3600 };
const POLL_MS = 15_000;
const C = {
  bg: "#0b0e14",
  grid: "#161b26",
  text: "#8b93a7",
  up: "#22c55e",
  down: "#ef4444",
  forming: "#4b5563",
  ema20: "#f5b942",
  ema50: "#60a5fa",
  rsi: "#c084fc",
  stop: "#ef4444",
  target: "#22c55e",
  entry: "#e5e7eb",
};

type Props = {
  trades: Trade[];
  position: Position | null;
  quote: { bid: string; ask: string; mid: string; time: string } | null;
  quoteFresh: boolean;
  now: number;
};

function barTime(iso: string, interval: Interval): UTCTimestamp {
  const s = Math.floor(new Date(iso).getTime() / 1000);
  return (s - (s % STEP[interval])) as UTCTimestamp;
}

export default function TerminalChart({ trades, position, quote, quoteFresh, now }: Props) {
  const [interval, setIv] = useState<Interval>("30m");
  const [data, setData] = useState<CandlesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const s = useRef<{
    candles: ISeriesApi<"Candlestick">;
    vol: ISeriesApi<"Histogram">;
    e20: ISeriesApi<"Line">;
    e50: ISeriesApi<"Line">;
    rsi: ISeriesApi<"Line">;
    markers: ISeriesMarkersPluginApi<Time>;
  } | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const fitted = useRef<string>("");

  const seq = useRef(0);
  const load = useCallback(async () => {
    const mine = ++seq.current;
    const days = interval === "30m" ? 10 : 20;
    const res = await getBackend<CandlesResponse>("candles", {
      interval,
      limit: "2000",
      start: new Date(Date.now() - days * 86_400_000).toISOString(),
    });
    if (mine !== seq.current) return;
    if (res.ok) {
      setData(res.data);
      setError(null);
      setLastOk(Date.now());
    } else setError(res.error);
  }, [interval]);

  useEffect(() => {
    const first = window.setTimeout(() => void load(), 0);
    const id = window.setInterval(() => void load(), POLL_MS);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(id);
    };
  }, [load]);

  useEffect(() => {
    if (!box.current) return;
    const c = createChart(box.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: C.bg },
        textColor: C.text,
        fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif",
        attributionLogo: true,
        panes: { separatorColor: C.grid, enableResize: false },
      },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      rightPriceScale: { borderColor: C.grid },
      timeScale: { borderColor: C.grid, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
      localization: {
        locale: "en-US",
        timeFormatter: (t: Time) => `${fmtUtc(new Date((t as number) * 1000).toISOString())} UTC`,
      },
    });
    const candles = c.addSeries(CandlestickSeries, {
      upColor: C.up,
      downColor: C.down,
      borderVisible: false,
      wickUpColor: C.up,
      wickDownColor: C.down,
      priceLineVisible: false,
    });
    const vol = c.addSeries(HistogramSeries, {
      priceScaleId: "vol",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.22 } });
    const lineOpts = { lineWidth: 1 as const, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    const e20 = c.addSeries(LineSeries, { ...lineOpts, color: C.ema20 });
    const e50 = c.addSeries(LineSeries, { ...lineOpts, color: C.ema50 });
    const r = c.addSeries(LineSeries, { ...lineOpts, color: C.rsi, lastValueVisible: true }, 1);
    r.createPriceLine({ price: 70, color: "#3f3f46", lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: "" });
    r.createPriceLine({ price: 30, color: "#3f3f46", lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: "" });
    const panes = c.panes();
    if (panes[1]) panes[1].setHeight(90);
    chart.current = c;
    s.current = { candles, vol, e20, e50, rsi: r, markers: createSeriesMarkers(candles, []) };
    return () => {
      c.remove();
      chart.current = null;
      s.current = null;
      lines.current = [];
    };
  }, []);

  // candles + indicators
  useEffect(() => {
    const ss = s.current;
    if (!ss || !data) return;
    const bars = [...data.candles];
    const forming = data.forming && (!bars.length || data.forming.t > bars[bars.length - 1].t) ? data.forming : null;
    const closes = bars.map((b) => Number(b.c));
    const e20 = ema(closes, 20);
    const e50 = ema(closes, 50);
    const r = rsi(closes, 14);
    const all = forming ? [...bars, forming] : bars;
    ss.candles.setData(
      all.map((b) => ({
        time: b.t as UTCTimestamp,
        open: +b.o,
        high: +b.h,
        low: +b.l,
        close: +b.c,
        ...(b === forming ? { color: C.forming, wickColor: C.forming, borderColor: C.forming } : {}),
      })),
    );
    ss.vol.setData(
      all.map((b) => ({
        time: b.t as UTCTimestamp,
        value: +b.v,
        color: b === forming ? C.forming : `${+b.c >= +b.o ? C.up : C.down}55`,
      })),
    );
    const line = (vals: (number | null)[]) =>
      bars.flatMap((b, i) => (vals[i] == null ? [] : [{ time: b.t as UTCTimestamp, value: vals[i] as number }]));
    ss.e20.setData(line(e20));
    ss.e50.setData(line(e50));
    ss.rsi.setData(line(r));
    const key = `${data.interval}`;
    if (fitted.current !== key) {
      chart.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, all.length - 120), to: all.length + 3 });
      fitted.current = key;
    }
  }, [data]);

  // virtual trades: markers + SL/TP/entry lines of the open position
  const markers = useMemo(() => {
    const out: SeriesMarker<Time>[] = [];
    for (const t of trades) {
      const long = t.side === "LONG";
      out.push({
        time: barTime(t.opened_at, interval),
        position: long ? "belowBar" : "aboveBar",
        color: long ? C.up : C.down,
        shape: long ? "arrowUp" : "arrowDown",
        text: `${t.side} ${fmtNum(t.entry_price, 3)}`,
      });
      if (t.closed_at && t.exit_price) {
        const win = Number(t.net_pnl) > 0;
        out.push({
          time: barTime(t.closed_at, interval),
          position: long ? "aboveBar" : "belowBar",
          color: win ? C.up : C.down,
          shape: "circle",
          text: `יציאה ${fmtNum(t.exit_price, 3)}`,
        });
      }
    }
    return out.sort((a, b) => (a.time as number) - (b.time as number));
  }, [trades, interval]);

  useEffect(() => {
    const ss = s.current;
    if (!ss) return;
    ss.markers.setMarkers(markers);
    for (const l of lines.current) ss.candles.removePriceLine(l);
    lines.current = [];
    if (position) {
      const mk = (price: string, color: string, title: string, style: LineStyle) =>
        ss.candles.createPriceLine({ price: +price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title });
      lines.current.push(mk(position.stop, C.stop, "SL", LineStyle.Dashed));
      lines.current.push(mk(position.target, C.target, "TP", LineStyle.Dashed));
      lines.current.push(mk(position.entry_price, C.entry, position.side, LineStyle.Dotted));
    }
    if (quote && quoteFresh) {
      lines.current.push(
        ss.candles.createPriceLine({ price: +quote.mid, color: "#94a3b8", lineWidth: 1, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: "mid" }),
      );
    }
  }, [markers, position, quote, quoteFresh, data]);

  const lastClosed = data?.candles[data.candles.length - 1];
  const closedAge = lastClosed ? ageFrom(lastClosed.close_time, now) : null;
  const pollAge = lastOk ? (now - lastOk) / 1000 : null;
  const connected = !error && pollAge != null && pollAge < 45;
  const stale = data != null && !data.fresh;

  return (
    <section className="t-card" aria-labelledby="t-chart">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-baseline gap-3">
          <h2 id="t-chart" className="text-sm font-semibold tracking-wide">
            <bdi dir="ltr">xyz:CL</bdi> · WTI
          </h2>
          <span className="t-price" dir="ltr">
            {quote && quoteFresh ? fmtNum(quote.mid, 3) : "—"}
          </span>
          {quote && quoteFresh ? (
            <span className="text-xs t-muted" dir="ltr">
              bid {fmtNum(quote.bid, 3)} · ask {fmtNum(quote.ask, 3)}
            </span>
          ) : (
            <span className="t-chip t-chip-bad">אין מחיר חי</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className={`t-chip ${connected ? "t-chip-ok" : "t-chip-bad"}`} role="status">
            <span className="t-dot" />
            {connected ? "מחובר" : "מנותק"}
          </span>
          <div className="t-seg" role="group" aria-label="טווח זמן לנר">
            {(["30m", "1h"] as Interval[]).map((iv) => (
              <button key={iv} type="button" aria-pressed={interval === iv} onClick={() => setIv(iv)}>
                <bdi dir="ltr">{iv}</bdi>
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="mb-1 flex flex-wrap gap-3 text-[11px] t-muted" dir="rtl">
        <span><i className="t-key" style={{ background: C.ema20 }} /> EMA 20</span>
        <span><i className="t-key" style={{ background: C.ema50 }} /> EMA 50</span>
        <span><i className="t-key" style={{ background: C.rsi }} /> RSI 14</span>
        <span><i className="t-key" style={{ background: C.stop }} /> SL</span>
        <span><i className="t-key" style={{ background: C.target }} /> TP</span>
        <span>▲ LONG · ▼ SHORT · ● יציאה</span>
      </div>
      <div dir="ltr" className="t-chart" ref={box} role="img" aria-label={`גרף ${interval} עם עסקאות וירטואליות`} />
      <div className="mt-2 flex flex-wrap justify-between gap-2 text-[11px] t-muted">
        <span>
          נר סגור אחרון: <bdi dir="ltr">{lastClosed ? `${fmtUtc(lastClosed.open_time)} UTC` : "—"}</bdi>
          {closedAge != null ? ` (נסגר לפני ${fmtAge(closedAge)})` : ""}
        </span>
        <span>מתעדכן כל 15 שנ׳ · האינדיקטורים לתצוגה בלבד</span>
      </div>
      {stale && (
        <p role="alert" className="mt-2 t-alert t-alert-warn">
          נתוני הנרות אינם עדכניים. ייתכן ששירות האיסוף אינו פעיל; הבוט לא ייכנס לעסקאות על נתונים ישנים.
        </p>
      )}
      {data && data.count === 0 && <p className="mt-2 t-alert t-alert-warn">אין נרות שמורים בטווח.</p>}
      {error && <p role="alert" className="mt-2 t-alert t-alert-bad">אין חיבור לנתוני הגרף ({error}). מנסה שוב…</p>}
    </section>
  );
}
