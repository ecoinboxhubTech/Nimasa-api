"""Chart exports (PNG, JPG, PDF) of a location's series. CSV stays in the router.
Mirrors the on-screen rules: gaps stay gaps (nothing is interpolated), forecasts are dashed, and every export names
its source, data type and generation time (FR-011)."""
import io
from datetime import datetime, timedelta

FORMATS = {"png": ("image/png", "png"), "jpg": ("image/jpeg", "jpeg"), "jpeg": ("image/jpeg", "jpeg"), "pdf": ("application/pdf", "pdf")}
GAP_FACTOR = 1.5  # a break longer than 1.5x the expected interval is drawn as a gap


def _segments(points, expected_minutes):
    """Split (time, value) pairs into runs so a missing stretch is a visible break, never a joined line."""
    limit = timedelta(minutes=expected_minutes * GAP_FACTOR)
    runs, cur = [], []
    for t, v in points:
        if cur and t - cur[-1][0] > limit:
            runs.append(cur)
            cur = []
        cur.append((t, v))
    if cur:
        runs.append(cur)
    return runs


def render_series(fmt: str, *, title: str, unit: str, points: list[tuple[datetime, float, str]], expected_minutes: int,
                  source_line: str, status: str, generated_at: datetime) -> bytes:
    from matplotlib.dates import DateFormatter
    from matplotlib.figure import Figure

    fig = Figure(figsize=(10, 5.6), dpi=150, facecolor="white")
    ax = fig.add_axes((0.08, 0.2, 0.89, 0.67))
    observed = [(t, v) for t, v, k in points if k != "forecast"]
    forecast = [(t, v) for t, v, k in points if k == "forecast"]
    if observed and forecast:
        forecast = [observed[-1]] + forecast if forecast[0][0] - observed[-1][0] <= timedelta(minutes=expected_minutes * GAP_FACTOR) else forecast
    for i, run in enumerate(_segments(observed, expected_minutes)):
        ax.plot(*zip(*run), color="#0b5fa5", lw=1.8, label="Observed / model estimate" if i == 0 else None)
    for i, run in enumerate(_segments(forecast, expected_minutes)):
        ax.plot(*zip(*run), color="#e07b00", lw=1.8, ls="--", label="Forecast" if i == 0 else None)
    if points:
        ax.axvline(generated_at, color="#6b7280", lw=1, ls=":")
        ax.legend(loc="lower right", bbox_to_anchor=(1, 1.01), ncol=2, frameon=False)
    else:
        ax.text(0.5, 0.5, "No data in the selected time range", ha="center", va="center", transform=ax.transAxes, color="#6b7280")
    ax.set_ylabel(unit)
    ax.grid(alpha=0.25)
    ax.xaxis.set_major_formatter(DateFormatter("%d %b\n%H:%M"))
    ax.set_xlabel("Time (UTC)")
    fig.suptitle(title, x=0.08, ha="left", fontsize=14, fontweight="bold")
    note = (f"{source_line}\nFeed status: {status}. Generated {generated_at:%Y-%m-%d %H:%M} UTC. "
            "Gaps are missing data and are not interpolated. Informational only; not an official forecast or warning.")
    fig.text(0.08, 0.02, note, fontsize=7.5, color="#374151", va="bottom")
    buf = io.BytesIO()
    kwargs = {"pil_kwargs": {"quality": 92}} if fmt == "jpeg" else {}
    fig.savefig(buf, format=fmt, **kwargs)
    return buf.getvalue()
