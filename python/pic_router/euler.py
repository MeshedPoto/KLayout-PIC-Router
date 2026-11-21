"""Pure-Python TOPIC (C3 Hermite) / partial-Euler (C2) bend math and multi-layer waveguide extrusion.

Zero numpy/scipy dependencies: runs identically inside KLayout 0.28–0.30+ embedded
Python 3.10+ (`pya`) and external Python (`klayout.db`).
"""

from __future__ import annotations

import math
from typing import Any

from .config import CrossSection, DEFAULT_DBU, get_kdb

# Positive half of 16-point Gauss-Legendre nodes and weights on [-1, 1]
_GL16_X: tuple[float, ...] = (
    0.09501250983763744,
    0.2816035507792589,
    0.45801677765722737,
    0.6178762444026438,
    0.755404408355003,
    0.8656312023878318,
    0.9445750230732326,
    0.9894009349916499,
)
_GL16_W: tuple[float, ...] = (
    0.18945061045506864,
    0.18260341504492364,
    0.16915651939500265,
    0.1495959888165767,
    0.12462897125553407,
    0.0951585116824926,
    0.062253523938647456,
    0.027152459411754176,
)
_HALF_PI: float = 0.5 * math.pi


def fresnel(u: float) -> tuple[float, float]:
    """Compute Fresnel integrals `(S(u), C(u))` matching `scipy.special.fresnel`.

    S(u) = integral_0^u sin(pi/2 * t^2) dt
    C(u) = integral_0^u cos(pi/2 * t^2) dt

    Accurate to < 1e-13 for all real u (and < 1e-14 across |u| <= 25).
    """
    u = float(u)
    if u == 0.0:
        return (0.0, 0.0)

    sign = 1.0 if u > 0.0 else -1.0
    au = abs(u)

    if au > 25.0:
        # Asymptotic auxiliary functions f(u), g(u) (A&S 7.3.27-7.3.28)
        z = math.pi * au * au
        inv_z2 = 1.0 / (z * z)
        f_sum = 1.0
        g_sum = 1.0
        f_term = 1.0
        g_term = 1.0
        for k in range(1, 7):
            f_term *= -(4 * k - 3) * (4 * k - 1) * inv_z2
            g_term *= -(4 * k - 1) * (4 * k + 1) * inv_z2
            f_sum += f_term
            g_sum += g_term
        f_val = f_sum / (math.pi * au)
        g_val = g_sum / (math.pi * z * au)
        phase = _HALF_PI * ((au * au) % 4.0)
        sp = math.sin(phase)
        cp = math.cos(phase)
        c_val = 0.5 + f_val * sp - g_val * cp
        s_val = 0.5 - f_val * cp - g_val * sp
        return (sign * s_val, sign * c_val)

    # Composite 16-point Gauss-Legendre with phase-bounded subintervals
    # Each subinterval advances phase (pi/2)*(b^2 - a^2) <= pi/2 (quarter period).
    u2 = au * au
    n_sub = max(1, int(math.ceil(u2)))
    du2 = u2 / n_sub

    s_total = 0.0
    c_total = 0.0
    t0 = 0.0
    gl_x = _GL16_X
    gl_w = _GL16_W
    sin = math.sin
    cos = math.cos
    sqrt = math.sqrt
    half_pi = _HALF_PI

    for i in range(1, n_sub + 1):
        t1 = au if i == n_sub else sqrt(i * du2)
        mid = 0.5 * (t0 + t1)
        half = 0.5 * (t1 - t0)
        s_sub = 0.0
        c_sub = 0.0
        for j in range(8):
            dx = half * gl_x[j]
            w = gl_w[j]
            tp = mid + dx
            tm = mid - dx
            ap = half_pi * tp * tp
            am = half_pi * tm * tm
            s_sub += w * (sin(ap) + sin(am))
            c_sub += w * (cos(ap) + cos(am))
        s_total += half * s_sub
        c_total += half * c_sub
        t0 = t1

    return (sign * s_total, sign * c_total)


def _unit_partial_euler_geometry(
    abs_theta: float, p: float
) -> tuple[float, float, float, float, float, float, float, float]:
    """Compute canonical CCW partial-Euler geometry for R_min = 1.0.

    Returns (dx1, dy1, x1, y1, x2, y2, u_p, a_scale) where:
      - (x1, y1): end of entry clothoid
      - (x2, y2): end of middle circular arc
      - (dx1, dy1): final bend endpoint for R_min = 1.0
      - u_p: Fresnel parameter at end of entry clothoid
      - a_scale: clothoid spatial scale factor for R_min = 1.0
    """
    if p == 0.0:
        dx1 = math.sin(abs_theta)
        dy1 = 1.0 - math.cos(abs_theta)
        return (dx1, dy1, 0.0, 0.0, dx1, dy1, 0.0, 0.0)

    theta_p = 0.5 * p * abs_theta
    u_p = math.sqrt(2.0 * theta_p / math.pi)
    a_scale = math.sqrt(2.0 * math.pi * theta_p)

    s_up, c_up = fresnel(u_p)
    x1 = a_scale * c_up
    y1 = a_scale * s_up

    # Middle circular arc of radius 1.0 from theta_1 = theta_p to theta_2 = abs_theta - theta_p
    theta_1 = theta_p
    theta_2 = abs_theta - theta_p
    xc = x1 - math.sin(theta_1)
    yc = y1 + math.cos(theta_1)
    x2 = xc + math.sin(theta_2)
    y2 = yc - math.cos(theta_2)

    # Symmetric exit unwind clothoid from theta_2 to abs_theta
    cos_t = math.cos(abs_theta)
    sin_t = math.sin(abs_theta)
    dx1 = x2 + x1 * cos_t + y1 * sin_t
    dy1 = y2 + x1 * sin_t - y1 * cos_t
    return (dx1, dy1, x1, y1, x2, y2, u_p, a_scale)


def euler_bend_metrics(
    angle_deg: float,
    radius: float,
    p: float = 0.5,
    use_eff_radius: bool = True,
) -> dict[str, float]:
    """Compute exact geometric metrics of a symmetric partial-Euler clothoid bend.

    Starts at (0, 0) with tangent (1, 0) and ends at (dx, dy) with tangent
    (cos(angle_deg), sin(angle_deg)). Positive angle_deg bends CCW (+y);
    negative angle_deg bends CW (-y).
    """
    angle_deg = float(angle_deg)
    radius = float(radius)
    p = float(p)

    if abs(angle_deg) < 1e-12 or abs(angle_deg) >= 360.0:
        raise ValueError(
            f"angle_deg must satisfy 0 < |angle_deg| < 360, got {angle_deg}"
        )
    if radius <= 0.0:
        raise ValueError(f"radius must be > 0, got {radius}")
    if not (0.0 <= p <= 1.0):
        raise ValueError(f"p must be in [0.0, 1.0], got {p}")

    sign = 1.0 if angle_deg > 0.0 else -1.0
    abs_theta = math.radians(abs(angle_deg))

    dx1, dy1, *_ = _unit_partial_euler_geometry(abs_theta, p)

    # For any symmetric curvature profile, chord vector points along abs_theta / 2
    half_theta = 0.5 * abs_theta
    chord_1 = dx1 * math.cos(half_theta) + dy1 * math.sin(half_theta)
    r_eff_unit = chord_1 / (2.0 * math.sin(half_theta))

    if use_eff_radius:
        r_eff = radius
        r_min = radius / r_eff_unit
    else:
        r_min = radius
        r_eff = radius * r_eff_unit

    dx = r_min * dx1
    dy = sign * r_min * dy1
    l_total = (1.0 + p) * r_min * abs_theta

    if abs(abs_theta - math.pi) < 1e-12:
        tangent_offset = float("inf")
    else:
        tangent_offset = r_eff * math.tan(half_theta)

    return {
        "angle_deg": angle_deg,
        "p": p,
        "R_eff": r_eff,
        "R_min": r_min,
        "L_total": l_total,
        "dx": dx,
        "dy": dy,
        "tangent_offset": tangent_offset,
    }


def euler_bend_points(
    angle_deg: float,
    radius: float,
    p: float = 0.5,
    use_eff_radius: bool = True,
    max_chord_err: float = 0.002,
    min_points: int = 33,
) -> list[tuple[float, float]]:
    """Sample a symmetric partial-Euler bend centerline starting at (0, 0) along (1, 0)."""
    metrics = euler_bend_metrics(
        angle_deg=angle_deg,
        radius=radius,
        p=p,
        use_eff_radius=use_eff_radius,
    )
    max_chord_err = max(float(max_chord_err), 1e-6)
    min_points = max(int(min_points), 5)

    sign = 1.0 if angle_deg > 0.0 else -1.0
    abs_theta = math.radians(abs(angle_deg))
    r_min = metrics["R_min"]
    dx_exact = metrics["dx"]
    dy_exact = metrics["dy"]

    # Maximum angular step on a circle of radius r_min for given chord error
    dtheta_chord = math.sqrt(8.0 * max_chord_err / r_min)
    ds_chord = r_min * dtheta_chord

    if p == 0.0:
        # Pure circular arc: refine first/last steps so endpoint tangents < 0.025 deg
        # while keeping interior spacing bounded by max_chord_err.
        end_step = min(math.radians(0.04), abs_theta / max(min_points - 1, 4))
        mid_span = abs_theta - 2.0 * end_step
        if mid_span <= 1e-9:
            n_seg = max(min_points - 1, 2)
            thetas = [abs_theta * i / n_seg for i in range(n_seg + 1)]
        else:
            n_mid = max(min_points - 3, int(math.ceil(mid_span / dtheta_chord)), 2)
            thetas = [0.0, end_step]
            for i in range(1, n_mid):
                thetas.append(end_step + mid_span * i / n_mid)
            thetas.append(abs_theta - end_step)
            thetas.append(abs_theta)

        pts: list[tuple[float, float]] = []
        for th in thetas:
            x = r_min * math.sin(th)
            y = sign * r_min * (1.0 - math.cos(th))
            pts.append((x, y))
        pts[0] = (0.0, 0.0)
        pts[-1] = (dx_exact, dy_exact)
        return pts

    dx1, dy1, x1_u, y1_u, x2_u, y2_u, u_p, a_unit = _unit_partial_euler_geometry(
        abs_theta, p
    )
    a_scale = r_min * a_unit
    x1 = r_min * x1_u
    y1 = r_min * y1_u
    x2 = r_min * x2_u
    y2 = r_min * y2_u

    theta_p = 0.5 * p * abs_theta
    theta_1 = theta_p
    theta_2 = abs_theta - theta_p
    theta_c = (1.0 - p) * abs_theta

    l_p = p * r_min * abs_theta
    l_c = (1.0 - p) * r_min * abs_theta

    # Entry/exit clothoid steps:
    # 1) Meet chord error ds_chord
    # 2) Ensure first/last chord angle (~ theta_p / (3 * n_e^2)) < 0.015 deg
    n_e_tangent = int(math.ceil(math.sqrt(math.degrees(theta_p) / 0.045)))
    n_e_chord = int(math.ceil(l_p / ds_chord))
    n_e_min = max(12, int(math.ceil((min_points - 1) * (0.5 if p >= 0.999 else 0.35))))
    n_e = max(n_e_tangent, n_e_chord, n_e_min)

    if theta_c > 1e-12:
        n_c_chord = int(math.ceil(l_c / ds_chord))
        n_c_min = max(4, min_points - 1 - 2 * n_e)
        n_c = max(n_c_chord, n_c_min, 2)
    else:
        n_c = 0

    pts = [(0.0, 0.0)]

    # 1. Entry clothoid: u from 0 -> u_p
    s_up, c_up = fresnel(u_p)
    for i in range(1, n_e + 1):
        u = u_p * i / n_e
        su, cu = fresnel(u)
        pts.append((a_scale * cu, sign * a_scale * su))

    # 2. Middle circular arc: theta from theta_1 -> theta_2
    if n_c > 0:
        xc = x1 - r_min * math.sin(theta_1)
        yc = y1 + r_min * math.cos(theta_1)
        for i in range(1, n_c + 1):
            th = theta_1 + theta_c * i / n_c
            x = xc + r_min * math.sin(th)
            y = yc - r_min * math.cos(th)
            pts.append((x, sign * y))

    # 3. Exit unwind clothoid: v from u_p -> 0
    cos_t = math.cos(abs_theta)
    sin_t = math.sin(abs_theta)
    for i in range(1, n_e + 1):
        v = u_p * (n_e - i) / n_e
        if i == n_e:
            pts.append((dx_exact, dy_exact))
        else:
            sv, cv = fresnel(v)
            dc = c_up - cv
            ds = s_up - sv
            x = x2 + a_scale * (cos_t * dc + sin_t * ds)
            y = y2 + a_scale * (sin_t * dc - cos_t * ds)
            pts.append((x, sign * y))

    pts[0] = (0.0, 0.0)
    pts[-1] = (dx_exact, dy_exact)
    return pts


VALID_BEND_TYPES: frozenset[str] = frozenset({"euler", "topic"})


def _compute_gl_nodes_weights_01(n: int = 32) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Compute Gauss-Legendre quadrature nodes & weights on [0, 1] in pure Python (float64)."""
    nodes = [0.0] * n
    weights = [0.0] * n
    m = (n + 1) // 2
    for i in range(m):
        z = math.cos(math.pi * (i + 0.75) / (n + 0.5))
        for _ in range(16):
            p1, p2 = 1.0, 0.0
            for j in range(1, n + 1):
                p3 = p2
                p2 = p1
                p1 = ((2.0 * j - 1.0) * z * p2 - (j - 1.0) * p3) / j
            pp = n * (z * p1 - p2) / (z * z - 1.0)
            dz = p1 / pp
            z -= dz
            if abs(dz) < 1e-16:
                break
        w = 2.0 / ((1.0 - z * z) * pp * pp)
        nodes[i] = 0.5 * (1.0 - z)
        nodes[n - 1 - i] = 0.5 * (1.0 + z)
        weights[i] = 0.5 * w
        weights[n - 1 - i] = 0.5 * w
    return tuple(nodes), tuple(weights)


_GL32_U, _GL32_W = _compute_gl_nodes_weights_01(32)


def _topic_top_xy_unit(theta_p: float, u_end: float) -> tuple[float, float]:
    """Integrate unit-R_c TOP segment (x/R_c, y/R_c) from u=0 to u_end in [0, 1] via 32-pt GL."""
    if theta_p <= 1e-15 or u_end <= 0.0:
        return (0.0, 0.0)
    scale = 2.0 * theta_p * u_end
    sx = 0.0
    sy = 0.0
    for u_gl, w_gl in zip(_GL32_U, _GL32_W):
        u = u_end * u_gl
        u2 = u * u
        th = theta_p * (u2 * u * (2.0 - u))
        sx += w_gl * math.cos(th)
        sy += w_gl * math.sin(th)
    return (scale * sx, scale * sy)


def _solve_topic_unit_geometry(abs_theta: float, p: float) -> tuple[float, float, float, float]:
    """Return ``(r_eff_unit, xo_hat, yo_hat, theta_p)`` for unit circular radius R_c = 1.

    Uses exact closed-form scale invariance of Deng et al. (Laser Photonics Rev. 2025):
        kappa(u) = (3*u^2 - 2*u^3) / R_c,  theta(u) = theta_p * (2*u^3 - u^4),  u in [0, 1]
    Guarantees C3 coordinate smoothness (continuous kappa AND dkappa/ds = 0 at all 4 junctions).
    """
    half_t = 0.5 * abs_theta
    theta_p = min(p * half_t, 0.5 * math.pi)
    if theta_p <= 1e-15:
        return (1.0, 0.0, 1.0, 0.0)

    x1_hat, y1_hat = _topic_top_xy_unit(theta_p, 1.0)
    xo_hat = x1_hat - math.sin(theta_p)
    yo_hat = y1_hat + math.cos(theta_p)
    denom = xo_hat * math.cos(half_t) + yo_hat * math.sin(half_t)
    sin_half = math.sin(half_t)
    if denom <= 1e-12 or sin_half <= 1e-12:
        raise ValueError(f"Non-physical TOPIC geometry for theta={math.degrees(abs_theta):.2f}deg, p={p}")
    r_eff_unit = denom / sin_half
    return (r_eff_unit, xo_hat, yo_hat, theta_p)


def topic_bend_metrics(
    angle_deg: float,
    radius: float,
    p: float = 0.5,
    use_eff_radius: bool = True,
) -> dict[str, float]:
    """Compute exact geometric metrics of a constant-width C3 TOPIC (TOP-CIRC-TOP') bend.

    Starts at (0, 0) with tangent (1, 0) and ends at (dx, dy) with tangent
    (cos(angle_deg), sin(angle_deg)), matching the exact footprint & tangent_offset
    of a circular/Euler bend of radius ``R_eff``. Parameter ``p in [0, 1]`` maps
    linearly to transition angle ``theta_p = 0.5 * p * |angle_deg|``:
      - ``p = 0.0`` -> pure circular arc (R_c = R_eff)
      - ``p = 0.5`` -> theta_p = 22.5 deg (for 90 deg bend)
      - ``p = 1.0`` -> pure cubic-curvature bend (L_CIRC = 0)
    """
    angle_deg = float(angle_deg)
    radius = float(radius)
    p = float(p)

    if abs(angle_deg) < 1e-12 or abs(angle_deg) >= 360.0:
        raise ValueError(
            f"angle_deg must satisfy 0 < |angle_deg| < 360, got {angle_deg}"
        )
    if radius <= 0.0:
        raise ValueError(f"radius must be > 0, got {radius}")
    if not (0.0 <= p <= 1.0):
        raise ValueError(f"p must be in [0.0, 1.0], got {p}")

    sign = 1.0 if angle_deg > 0.0 else -1.0
    abs_theta = math.radians(abs(angle_deg))
    half_theta = 0.5 * abs_theta

    r_eff_unit, xo_hat, yo_hat, theta_p = _solve_topic_unit_geometry(abs_theta, p)

    if use_eff_radius:
        r_eff = radius
        r_c = radius / r_eff_unit
    else:
        r_c = radius
        r_eff = radius * r_eff_unit

    l_top = 2.0 * r_c * theta_p
    l_circ = max(0.0, r_c * (abs_theta - 2.0 * theta_p))
    l_total = 2.0 * l_top + l_circ
    dx = r_eff * math.sin(abs_theta)
    dy = sign * r_eff * (1.0 - math.cos(abs_theta))

    if abs(abs_theta - math.pi) < 1e-12:
        tangent_offset = float("inf")
    else:
        tangent_offset = r_eff * math.tan(half_theta)

    return {
        "angle_deg": angle_deg,
        "p": p,
        "theta_p_deg": math.degrees(theta_p),
        "R_eff": r_eff,
        "R_min": r_c,
        "L_total": l_total,
        "L_top": l_top,
        "L_circ": l_circ,
        "x_o": r_c * xo_hat,
        "y_o": sign * r_c * yo_hat,
        "dx": dx,
        "dy": dy,
        "tangent_offset": tangent_offset,
    }


def topic_bend_points(
    angle_deg: float,
    radius: float,
    p: float = 0.5,
    use_eff_radius: bool = True,
    max_chord_err: float = 0.002,
    min_points: int = 33,
) -> list[tuple[float, float]]:
    """Sample an exact mirror-symmetric constant-width C3 TOPIC bend centerline."""
    metrics = topic_bend_metrics(
        angle_deg=angle_deg,
        radius=radius,
        p=p,
        use_eff_radius=use_eff_radius,
    )
    max_chord_err = max(float(max_chord_err), 1e-6)
    min_points = max(int(min_points), 5)

    sign = 1.0 if angle_deg > 0.0 else -1.0
    abs_theta = math.radians(abs(angle_deg))
    half_theta = 0.5 * abs_theta
    r_eff = metrics["R_eff"]
    r_c = metrics["R_min"]
    theta_p = math.radians(metrics["theta_p_deg"])
    l_top = metrics["L_top"]
    half_circ_angle = max(0.0, half_theta - theta_p)
    xo = metrics["x_o"]
    yo_pos = abs(metrics["y_o"])
    dx_exact = metrics["dx"]
    dy_exact = metrics["dy"]

    # Pure circular limit (p == 0): reuse refined circular sampling
    if theta_p <= 1e-15:
        return euler_bend_points(
            angle_deg=angle_deg,
            radius=r_eff,
            p=0.0,
            use_eff_radius=True,
            max_chord_err=max_chord_err,
            min_points=min_points,
        )

    dtheta_chord = math.sqrt(8.0 * max_chord_err / r_c)
    ds_chord = r_c * dtheta_chord

    n_top = max(12, int(math.ceil(l_top / ds_chord)))
    n_half_circ = (
        max(2, int(math.ceil(half_circ_angle / dtheta_chord)))
        if half_circ_angle > 1e-12
        else 0
    )
    if 2 * (n_top + n_half_circ) + 1 < min_points:
        scale = float(min_points) / max(2 * (n_top + n_half_circ) + 1, 1)
        n_top = max(n_top, int(math.ceil(n_top * scale)))
        if n_half_circ > 0:
            n_half_circ = max(n_half_circ, int(math.ceil(n_half_circ * scale)))

    # 1. Build first half (TOP + half CIRC) in canonical left-turn (+y) frame
    half_pts: list[tuple[float, float]] = [(0.0, 0.0)]
    for i in range(1, n_top + 1):
        u = i / n_top
        xu, yu = _topic_top_xy_unit(theta_p, u)
        half_pts.append((r_c * xu, r_c * yu))

    if n_half_circ > 0:
        for i in range(1, n_half_circ + 1):
            phi = theta_p + half_circ_angle * i / n_half_circ
            half_pts.append((xo + r_c * math.sin(phi), yo_pos - r_c * math.cos(phi)))

    # 2. Exact Householder reflection across bisector line n=(cos(theta/2), sin(theta/2)) through (0, R_eff)
    nx_b = math.cos(half_theta)
    ny_b = math.sin(half_theta)
    full_pos: list[tuple[float, float]] = list(half_pts)
    for rx, ry in reversed(half_pts[:-1]):
        sd = rx * nx_b + (ry - r_eff) * ny_b
        full_pos.append((rx - 2.0 * sd * nx_b, ry - 2.0 * sd * ny_b))

    pts = [(x, sign * y) for x, y in full_pos]
    pts[0] = (0.0, 0.0)
    pts[-1] = (dx_exact, dy_exact)
    return pts


def bend_metrics(
    angle_deg: float,
    radius: float,
    p: float = 0.5,
    use_eff_radius: bool = True,
    bend_type: str = "euler",
) -> dict[str, float]:
    """Unified metrics dispatcher supporting both ``'euler'`` (C2) and ``'topic'`` (C3) bends."""
    if str(bend_type or "euler").strip().lower() == "topic":
        return topic_bend_metrics(angle_deg=angle_deg, radius=radius, p=p, use_eff_radius=use_eff_radius)
    return euler_bend_metrics(angle_deg=angle_deg, radius=radius, p=p, use_eff_radius=use_eff_radius)


def bend_points(
    angle_deg: float,
    radius: float,
    p: float = 0.5,
    use_eff_radius: bool = True,
    max_chord_err: float = 0.002,
    min_points: int = 33,
    bend_type: str = "euler",
) -> list[tuple[float, float]]:
    """Unified centerline sampler supporting both ``'euler'`` (C2) and ``'topic'`` (C3) bends."""
    if str(bend_type or "euler").strip().lower() == "topic":
        return topic_bend_points(
            angle_deg=angle_deg,
            radius=radius,
            p=p,
            use_eff_radius=use_eff_radius,
            max_chord_err=max_chord_err,
            min_points=min_points,
        )
    return euler_bend_points(
        angle_deg=angle_deg,
        radius=radius,
        p=p,
        use_eff_radius=use_eff_radius,
        max_chord_err=max_chord_err,
        min_points=min_points,
    )


VALID_SBEND_CURVES: frozenset[str] = frozenset({"sbend_bezier", "sbend_sine"})


def sbend_bezier_params(length: float, offset: float) -> tuple[float, float]:
    """Return smooth unimodal-curvature control parameters (a, b) for C2 Quintic Bezier S-bend.

    Control points:
        P0=(0, 0), P1=(a*L, 0), P2=(b*L, 0),
        P3=((1-b)*L, H), P4=((1-a)*L, H), P5=(L, H)
    Because (P0, P1, P2) and (P3, P4, P5) are collinear along y=0 and y=H,
    endpoint tangents are strictly horizontal and endpoint curvatures are
    identically zero (kappa(0) = kappa(1) = 0) with C-infinity smoothness everywhere.
    """
    L = max(float(length), 1e-9)
    ratio = abs(float(offset)) / L
    a = 0.16
    b = min(0.66, 0.44 + 0.22 * min(ratio, 1.0))
    return (a, b)


def _sbend_eval(
    t: float,
    length: float,
    offset: float,
    curve_type: str = "sbend_bezier",
    bez_ab: tuple[float, float] | None = None,
) -> tuple[float, float, float, float, float, float]:
    """Evaluate `(x, y, dx, dy, ddx, ddy)` at parameter `t in [0, 1]` for a C2 zero-curvature S-bend."""
    if curve_type == "sbend_sine":
        # Raised-sine (cycloidal) S-bend: y(t) = H * (t - sin(2*pi*t)/(2*pi))
        # y'(0)=y'(1)=0, y''(0)=y''(1)=0 -> kappa(0)=kappa(1)=0, C-infinity everywhere.
        two_pi = 2.0 * math.pi
        ang = two_pi * t
        s = math.sin(ang)
        c = math.cos(ang)
        x = length * t
        y = offset * (t - s / two_pi)
        dx = length
        dy = offset * (1.0 - c)
        ddx = 0.0
        ddy = two_pi * offset * s
        return (x, y, dx, dy, ddx, ddy)

    # Default: C2 Quintic (degree-5) Bezier S-bend
    a, b = bez_ab if bez_ab is not None else sbend_bezier_params(length, offset)
    p0x, p1x, p2x = 0.0, a * length, b * length
    p3x, p4x, p5x = (1.0 - b) * length, (1.0 - a) * length, length
    # y control coords are [0, 0, 0, H, H, H]
    u = 1.0 - t
    u2 = u * u
    u3 = u2 * u
    u4 = u3 * u
    t2 = t * t
    t3 = t2 * t
    t4 = t3 * t

    x = (
        5.0 * u4 * t * p1x
        + 10.0 * u3 * t2 * p2x
        + 10.0 * u2 * t3 * p3x
        + 5.0 * u * t4 * p4x
        + t4 * t * p5x
    )
    y = offset * t3 * (10.0 * u2 + 5.0 * u * t + t2)

    dx = 5.0 * (
        u4 * (p1x - p0x)
        + 4.0 * u3 * t * (p2x - p1x)
        + 6.0 * u2 * t2 * (p3x - p2x)
        + 4.0 * u * t3 * (p4x - p3x)
        + t4 * (p5x - p4x)
    )
    dy = 30.0 * offset * u2 * t2

    ddx = 20.0 * (
        u3 * (p2x - 2.0 * p1x + p0x)
        + 3.0 * u2 * t * (p3x - 2.0 * p2x + p1x)
        + 3.0 * u * t2 * (p4x - 2.0 * p3x + p2x)
        + t3 * (p5x - 2.0 * p4x + p3x)
    )
    ddy = 60.0 * offset * u * t * (1.0 - 2.0 * t)
    return (x, y, dx, dy, ddx, ddy)


def _sbend_curvature(
    t: float,
    length: float,
    offset: float,
    curve_type: str,
    bez_ab: tuple[float, float] | None,
) -> tuple[float, float, float]:
    """Return `(kappa, speed, tangent_angle_rad)` at parameter `t in [0, 1]`."""
    _, _, dx, dy, ddx, ddy = _sbend_eval(t, length, offset, curve_type, bez_ab)
    sp2 = dx * dx + dy * dy
    if sp2 <= 1e-24:
        return (0.0, 0.0, 0.0)
    sp = math.sqrt(sp2)
    kappa = (dx * ddy - dy * ddx) / (sp2 * sp)
    ang = math.atan2(dy, dx)
    return (kappa, sp, ang)


def sbend_metrics(
    length: float,
    offset: float,
    curve_type: str = "sbend_bezier",
    n_eval: int = 128,
) -> dict[str, Any]:
    """Compute exact geometric metrics for a C2 zero-endpoint-curvature S-bend.

    Starts at (0, 0) along +x with kappa(0) = 0 and ends at (length, offset)
    along +x with kappa(1) = 0. Supports ``curve_type='sbend_bezier'`` (optimal
    degree-5 Quintic Bezier) and ``curve_type='sbend_sine'`` (raised-sine).
    """
    L = float(length)
    H = float(offset)
    if L <= 0.0:
        raise ValueError(f"S-bend length must be > 0, got {L}")
    if curve_type not in VALID_SBEND_CURVES:
        raise ValueError(
            f"Unsupported S-bend curve_type {curve_type!r}; expected one of {sorted(VALID_SBEND_CURVES)}"
        )

    if abs(H) < 1e-12:
        return {
            "curve_type": curve_type,
            "dx": L,
            "dy": 0.0,
            "L_total": L,
            "R_min": float("inf"),
            "R_eff": float("inf"),
            "max_angle_deg": 0.0,
            "kappa_start": 0.0,
            "kappa_end": 0.0,
        }

    bez_ab = sbend_bezier_params(L, H) if curve_type == "sbend_bezier" else None

    # Exact arc length via composite 16-point Gauss-Legendre on [0, 0.5] (x2 by symmetry)
    n_sub = 4
    dt = 0.5 / n_sub
    half_arc = 0.0
    for i in range(n_sub):
        t0 = i * dt
        mid = t0 + 0.5 * dt
        half = 0.5 * dt
        sub = 0.0
        for j in range(8):
            off_t = half * _GL16_X[j]
            w = _GL16_W[j]
            _, _, dxp, dyp, _, _ = _sbend_eval(mid + off_t, L, H, curve_type, bez_ab)
            _, _, dxm, dym, _, _ = _sbend_eval(mid - off_t, L, H, curve_type, bez_ab)
            sub += w * (math.hypot(dxp, dyp) + math.hypot(dxm, dym))
        half_arc += half * sub
    l_total = 2.0 * half_arc

    # Peak curvature on [0, 0.5] (antisymmetric on [0.5, 1.0]) + golden-section refinement
    n_half = max(32, int(n_eval) // 2)
    best_t = 0.25
    best_k = 0.0
    for i in range(n_half + 1):
        t = 0.5 * i / n_half
        k, _, _ = _sbend_curvature(t, L, H, curve_type, bez_ab)
        if abs(k) > best_k:
            best_k = abs(k)
            best_t = t

    step_t = 0.5 / n_half
    lo = max(0.0, best_t - step_t)
    hi = min(0.5, best_t + step_t)
    inv_phi = (math.sqrt(5.0) - 1.0) * 0.5
    for _ in range(28):
        m1 = hi - inv_phi * (hi - lo)
        m2 = lo + inv_phi * (hi - lo)
        k1 = abs(_sbend_curvature(m1, L, H, curve_type, bez_ab)[0])
        k2 = abs(_sbend_curvature(m2, L, H, curve_type, bez_ab)[0])
        if k1 > k2:
            hi = m2
            if k1 > best_k:
                best_k = k1
        else:
            lo = m1
            if k2 > best_k:
                best_k = k2

    r_min = (1.0 / best_k) if best_k > 1e-15 else float("inf")
    _, _, max_ang_rad = _sbend_curvature(0.5, L, H, curve_type, bez_ab)
    k0 = _sbend_curvature(0.0, L, H, curve_type, bez_ab)[0]
    k1_end = _sbend_curvature(1.0, L, H, curve_type, bez_ab)[0]

    return {
        "curve_type": curve_type,
        "dx": L,
        "dy": H,
        "L_total": l_total,
        "R_min": r_min,
        "R_eff": r_min,
        "max_angle_deg": abs(math.degrees(max_ang_rad)),
        "kappa_start": k0,
        "kappa_end": k1_end,
    }


def sbend_points(
    length: float,
    offset: float,
    curve_type: str = "sbend_bezier",
    max_chord_err: float = 0.002,
    min_points: int = 49,
) -> list[tuple[float, float]]:
    """Sample a C2 zero-endpoint-curvature S-bend centerline from (0, 0) to (length, offset)."""
    L = float(length)
    H = float(offset)
    if abs(H) < 1e-12:
        return [(0.0, 0.0), (L, 0.0)]

    metrics = sbend_metrics(L, H, curve_type=curve_type)
    r_min = float(metrics["R_min"])
    l_total = float(metrics["L_total"])
    max_chord_err = max(float(max_chord_err), 1e-6)
    min_points = max(int(min_points), 17)

    if math.isfinite(r_min) and r_min > 1e-6:
        ds_chord = math.sqrt(8.0 * r_min * max_chord_err)
        n_chord = int(math.ceil(1.35 * l_total / max(ds_chord, 1e-4)))
    else:
        n_chord = min_points - 1
    # Keep even number of intervals so midpoint t=0.5 is sampled explicitly
    n_seg = max(min_points - 1, n_chord, 32)
    if n_seg % 2 == 1:
        n_seg += 1

    bez_ab = sbend_bezier_params(L, H) if curve_type == "sbend_bezier" else None
    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    for i in range(1, n_seg):
        t = i / n_seg
        x, y, *_ = _sbend_eval(t, L, H, curve_type, bez_ab)
        pts.append((x, y))
    pts.append((L, H))
    return pts


def _dedupe_points(
    points: list[tuple[float, float]], eps: float = 1e-9
) -> list[tuple[float, float]]:
    """Remove consecutive duplicate or near-duplicate points."""
    cleaned: list[tuple[float, float]] = []
    for pt in points:
        x, y = float(pt[0]), float(pt[1])
        if not cleaned or math.hypot(x - cleaned[-1][0], y - cleaned[-1][1]) > eps:
            cleaned.append((x, y))
    return cleaned


def _prepare_tapered_polyline(
    points: list[tuple[float, float]],
    width: float,
    start_width: float | None = None,
    end_width: float | None = None,
    taper_length: float = 10.0,
) -> tuple[list[tuple[float, float]], list[float]]:
    """Insert exact taper transition vertices and compute width at each vertex."""
    pts = _dedupe_points(points)
    if len(pts) < 2:
        return pts, [float(width)] * len(pts)

    w_nom = float(width)
    w_start = w_nom if (start_width is None or float(start_width) <= 0.0) else float(start_width)
    w_end = w_nom if (end_width is None or float(end_width) <= 0.0) else float(end_width)
    taper_len = max(0.0, float(taper_length))

    # Cumulative arc lengths
    cum_s = [0.0]
    for i in range(1, len(pts)):
        cum_s.append(cum_s[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
    total_l = cum_s[-1]

    has_start_taper = abs(w_start - w_nom) > 1e-9 and taper_len > 0.0 and total_l > 1e-9
    has_end_taper = abs(w_end - w_nom) > 1e-9 and taper_len > 0.0 and total_l > 1e-9

    if has_start_taper and has_end_taper:
        tl_start = min(taper_len, 0.5 * total_l)
        tl_end = min(taper_len, 0.5 * total_l)
    elif has_start_taper:
        tl_start = min(taper_len, total_l)
        tl_end = 0.0
    elif has_end_taper:
        tl_start = 0.0
        tl_end = min(taper_len, total_l)
    else:
        return pts, [w_nom] * len(pts)

    # Collect cut distances to insert into polyline so straight segments taper cleanly
    cuts: list[float] = []
    if has_start_taper and 1e-6 < tl_start < total_l - 1e-6:
        cuts.append(tl_start)
    if has_end_taper and 1e-6 < (total_l - tl_end) < total_l - 1e-6:
        s_end_cut = total_l - tl_end
        if not cuts or abs(s_end_cut - cuts[0]) > 1e-6:
            cuts.append(s_end_cut)
    cuts.sort()

    if cuts:
        new_pts: list[tuple[float, float]] = [pts[0]]
        new_s: list[float] = [0.0]
        cut_idx = 0
        for i in range(1, len(pts)):
            s0 = cum_s[i - 1]
            s1 = cum_s[i]
            x0, y0 = pts[i - 1]
            x1, y1 = pts[i]
            seg_l = s1 - s0
            while cut_idx < len(cuts) and cuts[cut_idx] < s1 - 1e-7:
                sc = cuts[cut_idx]
                if sc > s0 + 1e-7 and seg_l > 1e-12:
                    frac = (sc - s0) / seg_l
                    new_pts.append((x0 + frac * (x1 - x0), y0 + frac * (y1 - y0)))
                    new_s.append(sc)
                cut_idx += 1
            new_pts.append((x1, y1))
            new_s.append(s1)
        pts = new_pts
        cum_s = new_s

    widths: list[float] = []
    for s in cum_s:
        if has_start_taper and s <= tl_start + 1e-9:
            frac = min(1.0, max(0.0, s / tl_start))
            w = w_start + frac * (w_nom - w_start)
        elif has_end_taper and s >= total_l - tl_end - 1e-9:
            frac = min(1.0, max(0.0, (total_l - s) / tl_end))
            w = w_end + frac * (w_nom - w_end)
        else:
            w = w_nom
        widths.append(w)

    return pts, widths


def extrude_centerline(
    points: list[tuple[float, float]],
    width: float,
    offset: float = 0.0,
    start_width: float | None = None,
    end_width: float | None = None,
    taper_length: float = 10.0,
) -> list[tuple[float, float]]:
    """Extrude a smooth centerline polyline into a closed CCW ribbon polygon.

    Supports optional linear start/end width taper and miter-spike limiting.
    """
    pts, widths = _prepare_tapered_polyline(
        points=points,
        width=width,
        start_width=start_width,
        end_width=end_width,
        taper_length=taper_length,
    )
    n = len(pts)
    if n < 2:
        raise ValueError("extrude_centerline requires at least 2 distinct points")

    off = float(offset)

    # Precompute unit tangents and left normals for each segment
    tangents: list[tuple[float, float]] = []
    normals: list[tuple[float, float]] = []
    for i in range(n - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        seg_l = math.hypot(dx, dy)
        tx, ty = dx / seg_l, dy / seg_l
        tangents.append((tx, ty))
        normals.append((-ty, tx))

    left_pts: list[tuple[float, float]] = []
    right_pts: list[tuple[float, float]] = []

    miter_limit = 2.0  # clamp extreme spikes on sharp corners

    for i in range(n):
        px, py = pts[i]
        d_left = off + 0.5 * widths[i]
        d_right = off - 0.5 * widths[i]

        if i == 0:
            nx, ny = normals[0]
            left_pts.append((px + d_left * nx, py + d_left * ny))
            right_pts.append((px + d_right * nx, py + d_right * ny))
        elif i == n - 1:
            nx, ny = normals[-1]
            left_pts.append((px + d_left * nx, py + d_left * ny))
            right_pts.append((px + d_right * nx, py + d_right * ny))
        else:
            n1x, n1y = normals[i - 1]
            n2x, n2y = normals[i]
            t1x, t1y = tangents[i - 1]
            t2x, t2y = tangents[i]
            cross = t1x * t2y - t1y * t2x
            mx = n1x + n2x
            my = n1y + n2y
            mlen = math.hypot(mx, my)
            if mlen < 1e-9:
                # 180-deg reversal fallback: bevel with both normals
                left_pts.append((px + d_left * n1x, py + d_left * n1y))
                left_pts.append((px + d_left * n2x, py + d_left * n2y))
                right_pts.append((px + d_right * n1x, py + d_right * n1y))
                right_pts.append((px + d_right * n2x, py + d_right * n2y))
                continue

            ux, uy = mx / mlen, my / mlen
            denom = ux * n1x + uy * n1y
            scale = 1.0 / denom if denom > 1e-6 else miter_limit + 1.0

            if scale <= miter_limit:
                left_pts.append((px + d_left * scale * ux, py + d_left * scale * uy))
                right_pts.append((px + d_right * scale * ux, py + d_right * scale * uy))
            else:
                # Sharp corner: bevel outer side, clamp inner side
                if cross > 0.0:
                    # Turning left: right side is outer, left side is inner
                    left_pts.append((px + d_left * miter_limit * ux, py + d_left * miter_limit * uy))
                    right_pts.append((px + d_right * n1x, py + d_right * n1y))
                    right_pts.append((px + d_right * n2x, py + d_right * n2y))
                else:
                    # Turning right: left side is outer, right side is inner
                    left_pts.append((px + d_left * n1x, py + d_left * n1y))
                    left_pts.append((px + d_left * n2x, py + d_left * n2y))
                    right_pts.append((px + d_right * miter_limit * ux, py + d_right * miter_limit * uy))

    # Return CCW polygon: right side forward, then left side backward
    return right_pts + list(reversed(left_pts))


def _min_local_radius_and_max_turn(
    pts: list[tuple[float, float]],
) -> tuple[float, float]:
    """Estimate minimum discrete radius of curvature and max turn angle (rad)."""
    n = len(pts)
    if n < 3:
        return (float("inf"), 0.0)

    min_r = float("inf")
    max_turn = 0.0
    for i in range(1, n - 1):
        ax, ay = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        bx, by = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        la = math.hypot(ax, ay)
        lb = math.hypot(bx, by)
        if la < 1e-12 or lb < 1e-12:
            continue
        dot = max(-1.0, min(1.0, (ax * bx + ay * by) / (la * lb)))
        turn = math.acos(dot)
        if turn > max_turn:
            max_turn = turn
        if turn > 1e-7:
            r_loc = 0.5 * (la + lb) / turn
            if r_loc < min_r:
                min_r = r_loc
    return (min_r, max_turn)


def _pts_to_kdb_polygon(
    kdb: Any, pts: list[tuple[float, float]], inv_dbu: float
) -> Any:
    """Convert float um polygon points into an integer-DBU kdb.Polygon."""
    kpts = [kdb.Point(int(round(x * inv_dbu)), int(round(y * inv_dbu))) for x, y in pts]
    return kdb.Polygon(kpts)


def _insert_robust_ribbon_into_region(
    kdb: Any,
    region: Any,
    pts: list[tuple[float, float]],
    widths: list[float],
    offset: float,
    inv_dbu: float,
) -> None:
    """Insert strictly convex CCW segment quads + outer circular join wedges.

    Guarantees ZERO bowtie self-intersections and ZERO holes even when
    layer half-width exceeds the bend's minimum curvature radius (e.g.
    a wide cladding trench around a tight bend).
    """
    n = len(pts)
    if n < 2:
        return

    tangents: list[tuple[float, float]] = []
    normals: list[tuple[float, float]] = []
    seg_lens: list[float] = []

    for i in range(n - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        seg_l = math.hypot(dx, dy)
        tx, ty = dx / seg_l, dy / seg_l
        tangents.append((tx, ty))
        normals.append((-ty, tx))
        seg_lens.append(seg_l)

    off = float(offset)
    max_arc_step = math.radians(2.0)

    # 1. Insert strictly convex CCW trapezoid/quad for every centerline segment
    for i in range(n - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        nx, ny = normals[i]
        w0 = widths[i]
        w1 = widths[i + 1]
        dl0 = off + 0.5 * w0
        dr0 = off - 0.5 * w0
        dl1 = off + 0.5 * w1
        dr1 = off - 0.5 * w1

        quad = [
            (x0 + dr0 * nx, y0 + dr0 * ny),
            (x1 + dr1 * nx, y1 + dr1 * ny),
            (x1 + dl1 * nx, y1 + dl1 * ny),
            (x0 + dl0 * nx, y0 + dl0 * ny),
        ]
        region.insert(_pts_to_kdb_polygon(kdb, quad, inv_dbu))

    # 2. Insert overlapping convex outer-arc wedges at every turning interior vertex
    for i in range(1, n - 1):
        t1x, t1y = tangents[i - 1]
        t2x, t2y = tangents[i]
        cross = t1x * t2y - t1y * t2x
        dot = max(-1.0, min(1.0, t1x * t2x + t1y * t2y))
        alpha = math.atan2(cross, dot)
        if abs(alpha) < 1e-6:
            continue

        px, py = pts[i]
        h = 0.5 * widths[i]
        n1x, n1y = normals[i - 1]
        n2x, n2y = normals[i]

        d_left = off + h
        d_right = off - h

        # Anchor slightly inside adjacent segments along the strip so integer
        # DBU rounding never leaves a 1-DBU pinhole sliver along the joint seam.
        d1 = min(0.45 * seg_lens[i - 1], 0.25 * h, 0.25)
        d2 = min(0.45 * seg_lens[i], 0.25 * h, 0.25)

        if d_left > 0.0 and d_right < 0.0:
            # Standard centered / near-centered strip (centerline inside strip)
            tan_half = math.tan(0.5 * abs(alpha))
            d_min = min(d1, d2)
            if alpha > 0.0:
                # Left turn: outer = RIGHT (-d_right), inner = LEFT (+d_left)
                r_out = -d_right
                r_in_max = d_left
                r_in = min(r_in_max, 0.45 * d_min / max(tan_half, 1e-12))
                # Inner offset points strictly inside quad[i-1] and quad[i]
                m1 = (px - d1 * t1x + r_in * n1x, py - d1 * t1y + r_in * n1y)
                m2 = (px + d2 * t2x + r_in * n2x, py + d2 * t2y + r_in * n2y)
                # Exact intersection of the two inner offset lines at distance r_in
                p_in = (
                    px - r_in * tan_half * t1x + r_in * n1x,
                    py - r_in * tan_half * t1y + r_in * n1y,
                )
                ang1 = math.atan2(-n1y, -n1x)
                n_steps = max(1, int(math.ceil(alpha / max_arc_step)))
                arc_pts = [
                    (
                        px + r_out * math.cos(ang1 + alpha * k / n_steps),
                        py + r_out * math.sin(ang1 + alpha * k / n_steps),
                    )
                    for k in range(n_steps + 1)
                ]
                wedge = [m1] + arc_pts + [m2, p_in]
            else:
                # Right turn: outer = LEFT (+d_left), inner = RIGHT (-d_right)
                r_out = d_left
                r_in_max = -d_right
                r_in = min(r_in_max, 0.45 * d_min / max(tan_half, 1e-12))
                m1 = (px - d1 * t1x - r_in * n1x, py - d1 * t1y - r_in * n1y)
                m2 = (px + d2 * t2x - r_in * n2x, py + d2 * t2y - r_in * n2y)
                p_in = (
                    px - r_in * tan_half * t1x - r_in * n1x,
                    py - r_in * tan_half * t1y - r_in * n1y,
                )
                ang1 = math.atan2(n1y, n1x)
                n_steps = max(1, int(math.ceil(abs(alpha) / max_arc_step)))
                # Emit in CCW order: m2 -> arc(ang2 -> ang1) -> m1 -> p_in
                arc_pts = [
                    (
                        px + r_out * math.cos(ang1 + alpha * k / n_steps),
                        py + r_out * math.sin(ang1 + alpha * k / n_steps),
                    )
                    for k in range(n_steps, -1, -1)
                ]
                wedge = [m2] + arc_pts + [m1, p_in]
            region.insert(_pts_to_kdb_polygon(kdb, wedge, inv_dbu))
        else:
            # Asymmetric strip lying entirely on one side of the centerline:
            # emit annular sector connecting segment i-1 end to segment i start.
            r_a = d_right
            r_b = d_left
            ang1 = math.atan2(n1y, n1x)
            n_steps = max(1, int(math.ceil(abs(alpha) / max_arc_step)))
            arc_outer = [
                (
                    px + r_b * math.cos(ang1 + alpha * k / n_steps),
                    py + r_b * math.sin(ang1 + alpha * k / n_steps),
                )
                for k in range(n_steps + 1)
            ]
            arc_inner = [
                (
                    px + r_a * math.cos(ang1 + alpha * k / n_steps),
                    py + r_a * math.sin(ang1 + alpha * k / n_steps),
                )
                for k in range(n_steps, -1, -1)
            ]
            poly_pts = arc_outer + arc_inner
            # Ensure CCW orientation via signed area
            area2 = 0.0
            for k in range(len(poly_pts)):
                x_a, y_a = poly_pts[k]
                x_b, y_b = poly_pts[(k + 1) % len(poly_pts)]
                area2 += x_a * y_b - x_b * y_a
            if area2 < 0.0:
                poly_pts.reverse()
            if abs(area2) > 1e-12:
                region.insert(_pts_to_kdb_polygon(kdb, poly_pts, inv_dbu))


def build_multilayer_waveguide_regions(
    points: list[tuple[float, float]],
    cross_section: CrossSection,
    dbu: float = DEFAULT_DBU,
    start_width: float | None = None,
    end_width: float | None = None,
    taper_length: float = 10.0,
) -> dict[tuple[int, int], Any]:
    """Build merged `kdb.Region` per `(layer, datatype)` for a multi-layer waveguide.

    Applies optional `start_width`/`end_width` taper ONLY to `role == "core"` layers.
    Uses bowtie-free convex segment-quad + outer-wedge union automatically whenever
    a layer's half-width approaches or exceeds local bend radius (e.g. wide
    cladding trenches on tight bends).
    """
    kdb = get_kdb()
    clean_pts = _dedupe_points(points)
    if len(clean_pts) < 2:
        raise ValueError(
            "build_multilayer_waveguide_regions requires at least 2 distinct points"
        )
    if dbu <= 0.0:
        raise ValueError(f"dbu must be > 0, got {dbu}")

    inv_dbu = 1.0 / float(dbu)
    min_r, max_turn = _min_local_radius_and_max_turn(clean_pts)
    regions: dict[tuple[int, int], Any] = {}

    for spec in cross_section.layers:
        if spec.width <= 0.0:
            continue

        key = (spec.layer, spec.datatype)
        reg = regions.get(key)
        if reg is None:
            reg = kdb.Region()
            regions[key] = reg

        if spec.role == "core":
            sw = start_width
            ew = end_width
        else:
            sw = None
            ew = None

        layer_pts, layer_widths = _prepare_tapered_polyline(
            points=clean_pts,
            width=spec.width,
            start_width=sw,
            end_width=ew,
            taper_length=taper_length,
        )
        max_extent = abs(spec.offset) + 0.5 * max(layer_widths)

        # Use single smooth ribbon for narrow layers far from curvature center;
        # use convex segment quads + outer wedges whenever width is significant
        # relative to curvature radius or corners are sharp.
        if max_extent < 0.35 * min_r and max_turn < math.radians(15.0):
            poly_pts = extrude_centerline(
                points=clean_pts,
                width=spec.width,
                offset=spec.offset,
                start_width=sw,
                end_width=ew,
                taper_length=taper_length,
            )
            reg.insert(_pts_to_kdb_polygon(kdb, poly_pts, inv_dbu))
        else:
            _insert_robust_ribbon_into_region(
                kdb=kdb,
                region=reg,
                pts=layer_pts,
                widths=layer_widths,
                offset=spec.offset,
                inv_dbu=inv_dbu,
            )

    for key, reg in list(regions.items()):
        regions[key] = reg.merged()

    return regions
