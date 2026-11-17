# Body Studio: тело-основа, ползунки и запись нового тела во все файлы игры.
#
# Основа - обычное тело V из игры или мод тела (EBB, Lush, EVE...). Меняем только туловище с ногами:
# шея, стык с руками и ступни остаются на месте, поэтому голова, руки (и импланты рук), обувь
# подходят как раньше. Изменения задаются полем сдвига в пространстве, поэтому одинаково ложатся
# на все файлы тела (варианты груди, FPP, татуировки, шрамы, тень) и не рвут швы.
import os, re, json, shutil, zipfile
import numpy as np
import common as C
from meshpos import MeshPos
from knn import Index

tr = C.tr
CORE = os.path.dirname(os.path.abspath(__file__))
PFA = C.PFA
SET_VERSION = '2'   # меняется, если меняется состав или формат папки основы (2: ноги под каблук)


def torso_of(gender='female'):
    return C.body_files(gender)['torso']


def arms_of(gender='female'):
    bf = C.body_files(gender)
    return {'arm_l': bf['arm_l'], 'arm_r': bf['arm_r']}


TORSO = torso_of('female')   # женские по умолчанию (старые вызовы)
ARMS = arms_of('female')


def gender_of(s):
    """Пол тела по set.json основы (или проекту)."""
    return 'male' if (s or {}).get('gender') == 'male' else 'female'


def s_torso(s):
    return torso_of(gender_of(s))


def s_arms(s):
    return arms_of(gender_of(s))


def vanilla_list(gender='female'):
    name = 'vanilla_male.txt' if gender == 'male' else 'vanilla_female.txt'
    with open(os.path.join(CORE, name), encoding='utf-8') as f:
        return [l.strip() for l in f if l.strip()]


# ---------- сбор файлов основы ----------

def _serialize_all(files, jdir):
    """{путь в игре: .mesh/.morphtarget на диске} -> {путь в игре: json}. Одним запуском WolvenKit,
    файлы под номерами (WolvenKit кладёт результат плоско, одинаковые имена бы затёрлись)."""
    src = C.work_dir('ser')
    os.makedirs(jdir, exist_ok=True)
    names = {}
    for i, (rel, f) in enumerate(sorted(files.items())):
        n = f'{i:03d}' + os.path.splitext(rel)[1]
        shutil.copyfile(C.lp(f), os.path.join(src, n))
        names[rel] = n
    C.wk('convert', 'serialize', src, '-o', jdir)
    C.rmtree(src)
    out = {}
    for rel, n in names.items():
        j = os.path.join(jdir, n + '.json')
        if os.path.isfile(j):
            out[rel] = j
    return out


def _vanilla_cache(progress=None, gender='female'):
    """Файлы обычного тела из игры (один раз): {путь в игре: .mesh на диске}."""
    raw = os.path.join(C.CACHE, 'vanilla_set' + ('_male' if gender == 'male' else ''))
    want = {r: r for r in vanilla_list(gender) + list(arms_of(gender).values())}
    have = {r: os.path.join(raw, r) for r in want if os.path.isfile(C.lp(os.path.join(raw, r)))}
    if len(have) < len(want):
        if progress:
            progress(5, tr('Достаю обычное тело из файлов игры (один раз)...',
                           'Extracting the vanilla body from the game files (once)...'))
        tmp = C.work_dir('game')
        got = C.extract(C.game_archive_dirs(), tmp, want)
        if torso_of(gender) not in got:
            C.rmtree(tmp)
            raise RuntimeError(tr('В файлах игры не нашлось тело V. Проверь путь к игре в настройках.',
                                  'The V body was not found in the game files. Check the game path in Settings.'))
        for r, f in got.items():
            os.makedirs(C.lp(os.path.dirname(os.path.join(raw, r))), exist_ok=True)
            shutil.copyfile(C.lp(f), C.lp(os.path.join(raw, r)))
        C.rmtree(tmp)
        have = {r: os.path.join(raw, r) for r in want if os.path.isfile(C.lp(os.path.join(raw, r)))}
    return have


EXTRA_SKIP = re.compile(r'(cyberware\\player|arms_hq|\\a0_|nails|\\h0_|\\he_|\\hx_|head|eye|teeth|hair|'
                        r'garment|weapon|\\fx\\|_shadow_proxy)')


def collect(project_dir, base_mod=None, progress=None, gender='female'):
    """Готовит папку основы проекта: base/set.json + JSON всех файлов, которые будем менять.
    base_mod - мод тела (dict из C.scan_mods / C.manual_mod) или None (обычное тело).
    gender - 'female' или 'male' (тело женского или мужского V)."""
    gender = 'male' if gender == 'male' else 'female'
    TORSO = torso_of(gender)
    bdir = os.path.join(project_dir, 'base')
    sfile = os.path.join(bdir, 'set.json')
    try:
        s = json.load(open(sfile, encoding='utf-8'))
        if s.get('version') == SET_VERSION and s.get('wk') == C.wk_version() and gender_of(s) == gender and \
                all(os.path.isfile(os.path.join(bdir, v['json'])) for v in s['files'].values()):
            return s
    except Exception:
        pass
    van = _vanilla_cache(progress, gender)
    files, src = dict(van), {r: 'game' for r in van}
    work = C.work_dir('base')
    try:
        if base_mod:
            if progress:
                progress(20, tr('Достаю тело из мода...', 'Extracting the body from the mod...'))
            listed = C._list_files(base_mod)
            known = {r.lower(): r for r in van}
            take = {}
            for f in listed:
                if not f.endswith(('.mesh', '.morphtarget')):
                    continue
                if f in known:
                    take[known[f]] = known[f]
                elif not EXTRA_SKIP.search(f):
                    take[f] = f   # своя деталь мода (швы, варианты груди) - проверим, лежит ли на теле
            got = C.extract(base_mod['archives'], os.path.join(work, 'mod'), take)
            for r, f in got.items():
                files[r], src[r] = f, 'mod'
        if progress:
            progress(40, tr('Читаю модели тела...', 'Reading the body models...'))
        C.rmtree(bdir)
        jsons = _serialize_all(files, bdir)
    finally:
        C.rmtree(work)
    if TORSO not in jsons:
        raise RuntimeError(tr('Не удалось прочитать туловище тела. Подробности в log.txt.',
                              'Could not read the body torso. Details in log.txt.'))
    # детали мода, которых нет в обычном теле: берём, только если они лежат на коже туловища
    tp, _, _ = MeshPos(jsons[TORSO]).lod0()
    idx = Index(tp)
    for r in [r for r in jsons if src[r] == 'mod' and r not in van]:
        try:
            p, _, _ = MeshPos(jsons[r]).lod0()
            if os.path.basename(r).startswith('l0_'):
                # ноги под обувь (каблук и т.п.): стопа поднята, поэтому сверяем только голень у колена
                hi = p[p[:, 2] > 0.30]
                p = hi if len(hi) > 20 else p
            _, d = idx.query(p[::max(1, len(p) // 400)], 1)
            ok = np.median(np.sqrt(d[:, 0])) < 0.006
        except Exception:
            ok = False
        if not ok:
            os.remove(jsons.pop(r))
    s = {'version': SET_VERSION, 'wk': C.wk_version(), 'gender': gender, 'mod': base_mod['path'] if base_mod else '',
         'files': {r: {'json': os.path.basename(j), 'src': src[r]} for r, j in jsons.items()}}
    with open(sfile, 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    return s


def set_json(project_dir, s, rel):
    return os.path.join(project_dir, 'base', s['files'][rel]['json'])


# ---------- геометрия ----------

def weld(pos, tol=20000):
    """Точки с одной позицией (швы UV) -> одна точка. (уникальные позиции, обратная карта)."""
    key = np.round(np.asarray(pos) * tol).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    return np.asarray(pos)[first], inv.ravel()


def edges_of(tris):
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    return np.unique(np.sort(e, 1), axis=0)


def boundary_verts(tris, n):
    """Точки на открытых краях сетки (ребро только у одного треугольника)."""
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), 1)
    u, c = np.unique(e, axis=0, return_counts=True)
    b = np.zeros(n, bool)
    b[u[c == 1].ravel()] = True
    return b


def smooth_field(vals, edges, n, iters, keep=0.5):
    """Сглаживание значений по сетке (среднее соседей)."""
    v = np.asarray(vals, np.float64).copy()
    deg = np.bincount(edges.ravel(), minlength=n).astype(np.float64)
    deg[deg == 0] = 1
    for _ in range(iters):
        acc = np.zeros_like(v)
        np.add.at(acc, edges[:, 0], v[edges[:, 1]])
        np.add.at(acc, edges[:, 1], v[edges[:, 0]])
        avg = acc / (deg[:, None] if v.ndim > 1 else deg)
        v = keep * v + (1 - keep) * avg
    return v


def smoothstep(x, a, b):
    t = np.clip((np.asarray(x, np.float64) - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


class Base:
    """Туловище основы (сваренное по швам), ориентиры и поля ползунков."""

    def __init__(self, torso_json, gender='female'):
        self.gender = 'male' if gender == 'male' else 'female'
        m = MeshPos(torso_json)
        pos, tris, _ = m.lod0()
        self.raw_pos = pos
        self.pos, self.inv = weld(pos)
        t = self.inv[tris]
        t = t[(t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])]
        self.tris = t
        self.edges = edges_of(t)
        self.n = len(self.pos)
        self.marks = self._landmarks()
        self.lock = self._lock()

    # ориентиры тела: ищем по самой форме, поэтому работает на любом теле, не только обычном
    def _landmarks(self):
        P = self.pos
        x, y, z = P[:, 0], P[:, 1], P[:, 2]
        mid = (np.abs(x) < 0.012) & (z > 0.6)
        crotch = float(z[mid].min()) if mid.any() else 0.93
        top = float(np.percentile(z, 99.8))
        neck = top - 0.035
        L = {'crotch': crotch, 'top': top, 'neck': neck}
        # кончик груди: середина самой выступающей вперёд части (а не одна крайняя точка у ложбинки)
        side = (x > 0.03) & (z > crotch + 0.3) & (z < neck - 0.05)
        ys = y[side]
        top_front = side.copy()
        # у мужского V самое выступающее - грудина между мышцами, поэтому полосу берём шире
        top_front[side] = ys > ys.max() - (0.025 if self.gender == 'male' else 0.012)
        a = P[top_front].mean(0)
        L['apex'] = [float(abs(a[0])), float(a[1]), float(a[2])]

        def slices(lo, hi, f):
            best, bz = None, None
            for zz in np.arange(lo, hi, 0.01):
                s = P[np.abs(z - zz) < 0.008]
                if len(s) < 6:
                    continue
                v = f(s)
                if best is None or v < best:
                    best, bz = v, zz
            return float(bz if bz is not None else (lo + hi) / 2)
        L['waist'] = slices(L['apex'][2] - 0.3, L['apex'][2] - 0.06, lambda s: s[:, 0].max() - s[:, 0].min())
        L['hip'] = slices(crotch, L['waist'] - 0.08, lambda s: -(s[:, 0].max() - s[:, 0].min()))
        back = (z > crotch - 0.08) & (z < L['waist'] - 0.08) & (np.abs(x) > 0.03)
        j = np.argmin(np.where(back, y, 9))
        L['butt'] = [float(abs(P[j, 0])), float(P[j, 1]), float(P[j, 2])]
        male = self.gender == 'male'   # у мужского V ноги длиннее относительно паха (сверено по сетке)
        L['knee'] = crotch * (0.555 if male else 0.49)
        L['thigh'] = (crotch + L['knee']) / 2 + 0.04
        L['calf'] = L['knee'] * (0.72 if male else 0.62)
        L['ankle'] = L['knee'] * 0.30
        # средняя линия туловища (y по высоте) и ног (x, y по высоте для каждой стороны)
        zs = np.arange(0.0, top + 0.02, 0.02)
        trunk_y, leg_x, leg_y = [], [], []
        for zz in zs:
            s = P[np.abs(z - zz) < 0.015]
            trunk_y.append(float((s[:, 1].min() + s[:, 1].max()) / 2) if len(s) else np.nan)
            r = s[s[:, 0] > 0.01]
            if len(r) and zz < crotch + 0.04:
                leg_x.append(float((r[:, 0].min() + r[:, 0].max()) / 2))
                leg_y.append(float((r[:, 1].min() + r[:, 1].max()) / 2))
            else:
                leg_x.append(np.nan)
                leg_y.append(np.nan)

        def fill(a):
            a = np.array(a, np.float64)
            ok = ~np.isnan(a)
            return np.interp(np.arange(len(a)), np.flatnonzero(ok), a[ok]) if ok.any() else np.zeros(len(a))
        L['zs'] = zs.tolist()
        L['trunk_y'] = fill(trunk_y).tolist()
        L['leg_x'] = fill(leg_x).tolist()
        L['leg_y'] = fill(leg_y).tolist()
        return L

    def _lock(self):
        """1 - точку можно менять, 0 - нельзя (шея, стык с руками, ступни), плавно между ними."""
        P = self.pos
        L = self.marks
        b = boundary_verts(self.tris, self.n)
        seam = b & (P[:, 2] > L['apex'][2] - 0.1)       # шея и проймы рук
        w = np.ones(self.n)
        if seam.any():
            _, d = Index(P[seam]).query(P, 1)
            w *= smoothstep(np.sqrt(d[:, 0]), 0.012, 0.075)
        w *= 1 - smoothstep(P[:, 2], L['neck'] - 0.06, L['neck'] - 0.01)
        w *= smoothstep(P[:, 2], L['ankle'] * 0.55, L['ankle'] * 1.25)
        return w

    # ---------- поля ползунков ----------
    def centers(self, P=None):
        """Центр сечения для каждой точки: у туловища - средняя линия, у ног - ось ноги (плавно между)."""
        P = self.pos if P is None else P
        L = self.marks
        zs = np.array(L['zs'])
        ty = np.interp(P[:, 2], zs, L['trunk_y'])
        lx = np.interp(P[:, 2], zs, L['leg_x']) * np.sign(P[:, 0])
        ly = np.interp(P[:, 2], zs, L['leg_y'])
        leg = (1 - smoothstep(P[:, 2], L['crotch'] - 0.02, L['crotch'] + 0.07)) * smoothstep(np.abs(P[:, 0]), 0.0, 0.045)
        c = np.zeros_like(P)
        c[:, 0] = lx * leg
        c[:, 1] = ty * (1 - leg) + ly * leg
        c[:, 2] = P[:, 2]
        return c, leg

    def fields(self):
        """{ключ ползунка: сдвиг каждой точки при значении +1}. Уже с замком (шея, руки, ступни)
        и сглаживанием по сетке - без изломов."""
        P = self.pos
        L = self.marks
        x, y, z = P[:, 0], P[:, 1], P[:, 2]
        c, leg = self.centers()
        r = P - c
        r[:, 2] = 0
        band = lambda z0, s: np.exp(-((z - z0) ** 2) / (2 * s * s))
        ax, ay, az = L['apex']
        bx, by, bz = L['butt']
        out = {}

        def blob(cent, sigma, k, dirmask=None):
            """Объём из центра cent наружу (грудь, ягодицы, живот)."""
            d = P - np.asarray(cent)
            g = np.exp(-(d ** 2).sum(1) / (2 * sigma * sigma))
            if dirmask is not None:
                g *= dirmask
            return k * g[:, None] * d

        front = smoothstep(y - (c[:, 1]), -0.01, 0.05)
        backm = smoothstep(-(y - c[:, 1]), 0.0, 0.06)
        upper = smoothstep(z, L['crotch'] + 0.12, L['crotch'] + 0.25)
        F = np.zeros_like(P)
        for sx in (1, -1):
            sidem = smoothstep(sx * x, -0.01, 0.03)
            F += blob((sx * ax, ay - 0.07, az + 0.005), 0.06, 0.55, front * sidem)
        # к середине груди не раздуваем: иначе на крупной груди (EVE, Lush) ложбинка заплывает
        inward = np.sign(x) * F[:, 0] < 0
        F[inward, 0] *= 0.15
        F *= (0.35 + 0.65 * smoothstep(np.abs(x), 0.0, 0.035))[:, None]
        if self.gender == 'male':   # грудные мышцы: шире и площе груди, центр ближе к плечу
            F = np.zeros_like(P)
            for sx in (1, -1):
                sidem = smoothstep(sx * x, -0.01, 0.03)
                F += blob((sx * (ax + 0.02), ay - 0.06, az + 0.015), 0.07, 0.45, front * sidem)
            inward = np.sign(x) * F[:, 0] < 0
            F[inward, 0] *= 0.15
        out['breast_size'] = F
        g = np.zeros(len(P))
        for sx in (1, -1):
            d = P - np.array((sx * ax, ay - 0.03, az))
            g += np.exp(-(d ** 2).sum(1) / (2 * 0.06 ** 2)) * front * smoothstep(sx * x, -0.01, 0.03)
        out['breast_lift'] = g[:, None] * np.array([0, 0.004, 0.028])
        out['breast_gap'] = g[:, None] * np.c_[np.sign(x) * 0.018, np.zeros(len(P)), np.zeros(len(P))]
        out['waist'] = 0.20 * (band(L['waist'], 0.065) * upper)[:, None] * r
        hb = band(L['hip'], 0.075)
        out['hips'] = np.c_[0.16 * hb * x, np.zeros(len(P)), np.zeros(len(P))]
        F = np.zeros_like(P)
        for sx in (1, -1):
            sidem = smoothstep(sx * x, 0.0, 0.035)
            F += blob((sx * (bx * 0.85), by + 0.075, bz + 0.01), 0.075, 0.42, backm * sidem)
        out['butt'] = F
        bc = (0.0, float(np.interp(L['waist'] - 0.09, np.array(L['zs']), L['trunk_y'])), L['waist'] - 0.09)
        out['belly'] = blob(bc, 0.085, 0.35, front * upper)
        legm = smoothstep(L['crotch'] + 0.05 - z, 0.0, 0.08)
        out['thighs'] = 0.22 * (band(L['thigh'], 0.11) * legm)[:, None] * r
        out['calves'] = 0.30 * (band(L['calf'], 0.075) * legm)[:, None] * r
        out['thigh_gap'] = (band(L['thigh'] + 0.06, 0.13) * legm * smoothstep(np.abs(x), 0.0, 0.03))[:, None] * \
            np.c_[np.sign(x) * 0.022, np.zeros(len(P)), np.zeros(len(P))]
        out['weight'] = 0.10 * r * smoothstep(z, L['ankle'], L['calf'])[:, None]

        # ---- тонкие настройки (кнопка «Тонкие настройки») ----
        zero = np.zeros(len(P))
        xonly = lambda v: np.c_[v, zero, zero]
        gauss = lambda cent, s: np.exp(-((P - np.asarray(cent)) ** 2).sum(1) / (2 * s * s))
        # грудь: нижний и верхний объём, выступание вперёд, ширина
        lo, up_, pr, wd = (np.zeros_like(P) for _ in range(4))
        for sx in (1, -1):
            sidem = front * smoothstep(sx * x, -0.01, 0.03)
            lo += blob((sx * ax, ay - 0.06, az - 0.035), 0.045, 0.5, sidem)
            up_ += blob((sx * ax, ay - 0.06, az + 0.04), 0.045, 0.45, sidem)
            g = gauss((sx * ax, ay, az), 0.05) * sidem
            pr += g[:, None] * np.array([0, 0.02, 0])
            g = gauss((sx * ax, ay - 0.03, az), 0.06) * sidem
            wd += xonly(0.45 * g * (x - sx * ax))
        for F in (lo, up_, wd):   # к ложбинке не раздуваем (как у размера груди)
            inward = np.sign(x) * F[:, 0] < 0
            F[inward, 0] *= 0.15
        out['breast_lower'], out['breast_upper'], out['breast_project'], out['breast_width'] = lo, up_, pr, wd
        # туловище
        bm = np.zeros(len(P))   # сами груди грудная клетка не трогает
        for sx in (1, -1):
            bm += gauss((sx * ax, ay - 0.03, az), 0.055) * front
        out['ribcage'] = xonly(0.18 * band(az - 0.08, 0.05) * upper * (1 - np.clip(bm, 0, 1)) * x)
        W = lambda z0: 0.20 * (band(z0, 0.065) * upper)[:, None] * r
        out['waist_height'] = W(L['waist'] - 0.04) - W(L['waist'] + 0.04)
        out['flanks'] = xonly(0.18 * band(L['waist'] - 0.05, 0.06) * smoothstep(np.abs(x), 0.04, 0.12) * x)
        out['lower_back'] = np.c_[zero, 0.02 * band(L['waist'] - 0.03, 0.06) * backm, zero]
        zb = L['crotch'] + 0.12
        bl = (0.0, float(np.interp(zb, np.array(L['zs']), L['trunk_y'])), zb)
        out['belly_lower'] = blob(bl, 0.07, 0.35, front * smoothstep(z, L['crotch'] + 0.04, L['crotch'] + 0.1))
        # ягодицы: подъём, ширина, нижняя часть
        bu, bw, bd = (np.zeros_like(P) for _ in range(3))
        for sx in (1, -1):
            sidem = backm * smoothstep(sx * x, 0.0, 0.035)
            cb = (sx * bx * 0.85, by + 0.075, bz + 0.01)
            g = gauss(cb, 0.075) * sidem
            bu += g[:, None] * np.array([0, 0.004, 0.022])
            bw += xonly(0.35 * g * (x - sx * bx * 0.85))
            bd += blob((sx * bx * 0.85, by + 0.075, bz - 0.04), 0.05, 0.4, sidem)
        out['butt_lift'], out['butt_width'], out['butt_lower'] = bu, bw, bd
        # ноги: внутренняя и внешняя сторона бедра, колени
        # сторона ноги по углу вокруг её оси: плавно, без складок даже на очень плотной сетке (EVE)
        cos = np.sign(x) * r[:, 0] / (np.linalg.norm(r, axis=1) + 1e-6)
        side_in = ((1 - cos) / 2) ** 2
        side_out = ((1 + cos) / 2) ** 2
        out['thigh_inner'] = 0.25 * (band(L['thigh'] + 0.05, 0.10) * legm * side_in)[:, None] * r
        out['thigh_outer'] = 0.25 * (band(L['thigh'] + 0.08, 0.09) * legm * side_out)[:, None] * r
        out['knees'] = 0.25 * (band(L['knee'], 0.04) * legm)[:, None] * r
        for k in out:
            out[k] = smooth_field(out[k] * self.lock[:, None], self.edges, self.n, 6) * self.lock[:, None]
        return out


SLIDERS = [  # (ключ, русский, английский)
    ('weight', 'Полнота', 'Body weight'),
    ('breast_size', 'Грудь: размер', 'Breast size'),
    ('breast_lift', 'Грудь: подъём', 'Breast lift'),
    ('breast_gap', 'Грудь: расстановка', 'Breast spacing'),
    ('waist', 'Талия', 'Waist'),
    ('belly', 'Живот', 'Belly'),
    ('hips', 'Бёдра (таз)', 'Hips'),
    ('butt', 'Ягодицы', 'Butt'),
    ('thighs', 'Бёдра (ноги)', 'Thighs'),
    ('thigh_gap', 'Расстановка ног', 'Thigh gap'),
    ('calves', 'Икры', 'Calves'),
    # тонкие настройки
    ('breast_lower', 'Грудь: низ', 'Breast: lower volume'),
    ('breast_upper', 'Грудь: верх', 'Breast: upper volume'),
    ('breast_project', 'Грудь: вперёд', 'Breast: projection'),
    ('breast_width', 'Грудь: ширина', 'Breast: width'),
    ('ribcage', 'Грудная клетка', 'Ribcage'),
    ('waist_height', 'Талия: выше/ниже', 'Waist height'),
    ('flanks', 'Бока', 'Flanks'),
    ('lower_back', 'Прогиб поясницы', 'Lower back arch'),
    ('belly_lower', 'Низ живота', 'Lower belly'),
    ('butt_lift', 'Ягодицы: подъём', 'Butt: lift'),
    ('butt_width', 'Ягодицы: ширина', 'Butt: width'),
    ('butt_lower', 'Ягодицы: низ', 'Butt: lower volume'),
    ('thigh_inner', 'Бедро внутри', 'Inner thigh'),
    ('thigh_outer', 'Бедро снаружи', 'Outer thigh'),
    ('knees', 'Колени', 'Knees'),
]


# мужской V: вместо груди - грудные мышцы; часть женских ползунков груди не показываем
MALE_LABELS = {'breast_size': ('Грудные мышцы', 'Pecs'),
               'breast_project': ('Грудные: вперёд', 'Pecs: projection'),
               'breast_width': ('Грудные: ширина', 'Pecs: width')}
MALE_HIDDEN = {'breast_lift', 'breast_gap', 'breast_lower', 'breast_upper'}


def slider_label(k, gender='female'):
    if gender == 'male' and k in MALE_LABELS:
        return tr(*MALE_LABELS[k])
    for key, ru, en in SLIDERS:
        if key == k:
            return tr(ru, en)
    return k


def slider_visible(k, gender='female'):
    return not (gender == 'male' and k in MALE_HIDDEN)


# ---------- проверка перед экспортом ----------

CHECK = {'flip': 0.0,      # треугольник развернулся больше чем на 90°
         'sharp': 0.45,    # бугор/вмятина: сдвиг точки от среднего соседей больше 0.45 длины ребра
         'arm': 0.002,     # тело зашло в руку глубже 2 мм (и раньше там не было)
         'far': 0.10}      # точка ушла дальше 10 см от основы


def face_normals(P, T):
    n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
    return n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)


def vertex_normals(P, T):
    n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
    vn = np.zeros_like(P)
    for i in range(3):
        np.add.at(vn, T[:, i], n)
    return vn / (np.linalg.norm(vn, axis=1, keepdims=True) + 1e-12)


def zone_name(base, z):
    L = base.marks
    if z > L['apex'][2] + 0.06:
        return tr('плечи', 'shoulders')
    if z > L['waist'] + 0.05:
        return tr('грудь', 'chest')
    if z > L['hip'] + 0.03:
        return tr('талия', 'waist')
    if z > L['crotch'] - 0.05:
        return tr('таз', 'hips')
    return tr('ноги', 'legs')


def check_shape(base, disp, arms=()):
    """Ищет то, что в игре будет выглядеть плохо. disp - сдвиг каждой точки от основы,
    arms - [(точки, треугольники)] рук основы. Возвращает {вид: индексы точек}."""
    P0, T, E = base.pos, base.tris, base.edges
    P = P0 + disp
    out = {}
    # 1) вывернутые треугольники
    dots = (face_normals(P0, T) * face_normals(P, T)).sum(1)
    bad = T[dots < CHECK['flip']].ravel()
    out['flip'] = np.unique(bad)
    # 2) резкие места: точка сильно выбивается из соседей (по сравнению с основой)
    deg = np.bincount(E.ravel(), minlength=base.n).astype(np.float64)
    deg[deg == 0] = 1

    def lap(Q):
        acc = np.zeros_like(Q)
        np.add.at(acc, E[:, 0], Q[E[:, 1]])
        np.add.at(acc, E[:, 1], Q[E[:, 0]])
        return Q - acc / deg[:, None]
    el = np.linalg.norm(P0[E[:, 0]] - P0[E[:, 1]], axis=1)
    elv = np.zeros(base.n)
    np.add.at(elv, E[:, 0], el)
    np.add.at(elv, E[:, 1], el)
    elv /= deg
    ratio = np.linalg.norm(lap(P) - lap(P0), axis=1) / (elv + 1e-9)
    out['sharp'] = np.where(ratio > CHECK['sharp'])[0]
    # 3) тело влезло в руки
    hits = []
    if arms:
        from mathutils import kdtree
        cand = np.where((np.linalg.norm(disp, axis=1) > 0.0005) & (P0[:, 2] > base.marks['waist']))[0]
        for ap, at in arms:
            ap = np.asarray(ap, np.float64)
            an = vertex_normals(ap, np.asarray(at))
            if (an * (ap - ap.mean(0))).sum(1).mean() < 0:   # нормали наружу
                an = -an
            kd = kdtree.KDTree(len(ap))
            for i, p in enumerate(ap):
                kd.insert(p, i)
            kd.balance()
            for i in cand:
                co, j, dist = kd.find(P[i])
                if dist > 0.03:
                    continue
                now = float(np.dot(P[i] - ap[j], an[j]))
                was = float(np.dot(P0[i] - ap[j], an[j]))
                if now < -CHECK['arm'] and now < was - CHECK['arm']:
                    hits.append(i)
    out['arm'] = np.array(sorted(set(hits)), np.int64)
    # 4) слишком далеко от основы
    out['far'] = np.where(np.linalg.norm(disp, axis=1) > CHECK['far'])[0]
    return out


def check_messages(base, found):
    """Понятные строки для человека; пусто - всё хорошо."""
    names = {'flip': tr('вывернутые треугольники (в игре будут чёрные пятна или дырки)',
                        'flipped triangles (black spots or holes in the game)'),
             'sharp': tr('резкие бугры или складки', 'sharp bumps or creases'),
             'arm': tr('тело залезает в руки (в игре будет просвечивать)', 'the body goes into the arms (will clip in the game)'),
             'far': tr('слишком сильные изменения (больше 10 см), одежда может не подойти',
                       'very large changes (over 10 cm), clothing may not fit')}
    msgs = []
    for k in ('flip', 'arm', 'sharp', 'far'):
        idx = found.get(k, ())
        if len(idx):
            zs = {}
            for z in base.pos[idx, 2]:
                n = zone_name(base, z)
                zs[n] = zs.get(n, 0) + 1
            where = ', '.join(n for n, _ in sorted(zs.items(), key=lambda a: -a[1]))
            msgs.append(f'{names[k]}: {where}')
    return msgs


# ---------- запись нового тела ----------

def vertex_normals(pos, tris):
    tris = tris[(tris < len(pos)).all(1)]
    fn = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
    vn = np.zeros_like(pos)
    for k in range(3):
        np.add.at(vn, tris[:, k], fn)
    key = np.round(pos * 20000).astype(np.int64)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.ravel()
    acc = np.zeros((inv.max() + 1, 3))
    np.add.at(acc, inv, vn)
    vn = acc[inv]
    return vn / (np.linalg.norm(vn, axis=1, keepdims=True) + 1e-12)


def update_normals(m, ci, old, new):
    """Поворачивает нормали/касательные на столько, на сколько изменилась форма."""
    n, nw = m.read_dec4(ci, 'PS_Normal')
    if n is None:
        return
    tris = m.triangles(ci)
    n2 = n + vertex_normals(new, tris) - vertex_normals(old, tris)
    n2 /= np.linalg.norm(n2, axis=1, keepdims=True) + 1e-12
    m.write_dec4(ci, 'PS_Normal', n2, nw)
    t, tw = m.read_dec4(ci, 'PS_Tangent')
    if t is not None:
        t2 = t - n2 * (t * n2).sum(1)[:, None]
        t2 /= np.linalg.norm(t2, axis=1, keepdims=True) + 1e-12
        m.write_dec4(ci, 'PS_Tangent', t2, tw)


def _update_bbox(m, allpos):
    root = m.j['Data']['RootChunk']
    bb = root.get('boundingBox')
    if not isinstance(bb, dict) or 'Min' not in bb:
        return
    lo, hi = allpos.min(0), allpos.max(0)
    for k, v in zip('XYZ', range(3)):
        bb['Min'][k] = float(min(bb['Min'][k], lo[v]))
        bb['Max'][k] = float(max(bb['Max'][k], hi[v]))


class Skin:
    """Кожа для переноса сдвига на остальные файлы: туловище (со сдвигом) + руки (без сдвига)."""

    def __init__(self, base, disp_welded, arm_jsons):
        pts = [base.pos]
        ds = [disp_welded]
        for j in arm_jsons:
            try:
                p, _, _ = MeshPos(j).lod0()
            except Exception:
                continue
            pts.append(p)
            ds.append(np.zeros_like(p))
        self.pos = np.concatenate(pts)
        self.disp = np.concatenate(ds)
        self.index = Index(self.pos)

    def at(self, pts, k=4):
        idx, dd = self.index.query(pts, k)
        w = 1.0 / (dd + 1e-10)
        w /= w.sum(1, keepdims=True)
        return (self.disp[idx] * w[..., None]).sum(1)


def deform_file(json_in, json_out, base, disp_raw, skin):
    """Новые позиции для одного файла тела. Файлы с той же сеткой, что у туловища (варианты груди,
    FPP-туловище, морфтаргет) - сдвиг точка в точку; остальные - по ближайшей коже."""
    m = MeshPos(json_in)
    olds = [m.read(ci) for ci in range(len(m.chunks))]
    lod0 = [ci for ci in range(len(m.chunks)) if m.lod_mask(ci) & 1]
    n0 = sum(len(olds[ci]) for ci in lod0)
    same = n0 == len(base.raw_pos) and len(lod0) and \
        np.median(np.linalg.norm(np.concatenate([olds[ci] for ci in lod0]) - base.raw_pos, axis=1)) < 0.003
    news, a = [], 0
    for ci, old in enumerate(olds):
        if same and ci in lod0:
            news.append(old + disp_raw[a:a + len(old)])
            a += len(old)
        else:
            news.append(old + skin.at(old))
    allpos = np.concatenate(news)
    m.set_quant(allpos)
    for ci, p in enumerate(news):
        m.write(ci, p)
        update_normals(m, ci, olds[ci], p)
    _update_bbox(m, allpos)
    m.save(json_out)
    return float(max(np.abs(n - o).max() for n, o in zip(news, olds)))


def build_mod(project_dir, s, disp_welded, name, tag='', prog=None, base=None):
    """Собирает мод тела: zip c archive/pc/mod/#BODY_<имя>.archive (+ .xl, если свой тег).
    Возвращает (путь zip, путь .archive, папка с готовыми файлами)."""
    base = base or Base(set_json(project_dir, s, s_torso(s)), gender_of(s))
    disp_raw = disp_welded[base.inv]
    ARMS = s_arms(s)
    arm_jsons = [set_json(project_dir, s, r) for r in ARMS.values() if r in s['files']]
    skin = Skin(base, disp_welded, arm_jsons)
    out = os.path.join(project_dir, 'out')
    C.rmtree(out)
    arch = '#BODY_' + C.safe(name, 32)
    root = os.path.join(out, 'p', arch)
    jtmp = os.path.join(out, 'j')
    os.makedirs(jtmp)
    todo = [r for r in s['files'] if r not in ARMS.values()]
    jobs = []
    for i, rel in enumerate(sorted(todo)):
        if prog:
            prog(10 + 60 * i / len(todo), os.path.basename(rel))
        jo = os.path.join(jtmp, f'{i:03d}' + os.path.splitext(rel)[1] + '.json')
        moved = deform_file(set_json(project_dir, s, rel), jo, base, disp_raw, skin)
        if moved < 1e-6 and s['files'][rel]['src'] == 'game':
            os.remove(jo)  # файл не изменился - в мод не кладём, игра возьмёт свой
            continue
        jobs.append((rel, jo))
    if prog:
        prog(72, tr('сборка файлов', 'building files'))
    if not jobs:
        raise RuntimeError(tr('Тело не отличается от основы - сначала подвигай ползунки или полепи.',
                              'The body is the same as the base - move some sliders or sculpt first.'))
    C.wk('convert', 'deserialize', jtmp, '-o', jtmp)
    for rel, jo in jobs:
        built = jo[:-5]
        if not os.path.isfile(built):
            raise RuntimeError(tr('WolvenKit не собрал ', 'WolvenKit could not build ') + os.path.basename(rel))
        dst = os.path.join(root, rel)
        os.makedirs(C.lp(os.path.dirname(dst)), exist_ok=True)
        shutil.copyfile(built, C.lp(dst))
    if prog:
        prog(85, tr('упаковка архива', 'packing the archive'))
    C.wk('pack', root, '-o', os.path.join(out, 'p'))
    os.makedirs(C.EXPORTS, exist_ok=True)
    zip_path = os.path.join(C.EXPORTS, C.fs_name(name)[:120] + ' (Body Studio).zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(root + '.archive', 'archive/pc/mod/' + arch + '.archive')
        if tag:
            z.writestr('archive/pc/mod/' + arch + '.xl',
                       f'# Body Studio: {name}\nplayer:\n  bodyTypes: [ {tag} ]\n')
    return zip_path, root + '.archive', root


# ---------- проверка, что всё нужное включено в игре ----------

def _material_refs(json_path):
    """Пути материалов (.mi/.mt), на которые ссылается меш: {хэш: путь или None}."""
    r = json.load(open(json_path, encoding='utf-8'))['Data']['RootChunk']
    refs = []
    for x in r.get('externalMaterials') or []:
        refs.append(x.get('DepotPath', {}).get('$value'))
    for m in (r.get('localMaterialBuffer') or {}).get('materials') or []:
        refs.append((m.get('baseMaterial') or {}).get('DepotPath', {}).get('$value'))
    for m in r.get('preloadLocalMaterialInstances') or []:
        refs.append(((m.get('Data') or {}).get('baseMaterial') or {}).get('DepotPath', {}).get('$value'))
    out = {}
    for v in refs:
        if v in (None, '', '0', 0):
            continue
        if isinstance(v, int) or str(v).isdigit():
            out[int(v)] = None
        else:
            out[C.fnv1a64(v)] = v
    return out


def _hashes(archives):
    hs = set()
    for a in archives:
        try:
            hs |= C.archive_hashes(a)
        except Exception:
            pass
    return hs


def check_deployed(project_dir, s, base_mod):
    """Что не так в игре для этого тела: список понятных предупреждений (пустой - всё в порядке).
    1) мод-основа выключен; 2) файлы основы ссылаются на материалы кожи, которых в игре нет
    (обычно их даёт отдельный мод, например UV Framework) - без них части тела невидимы."""
    if not base_mod:
        return []
    cfg = C.load_config()
    game = cfg.get('game', '')
    moddir = os.path.join(game, 'archive', 'pc', 'mod')
    warn = []
    deployed = {}
    if os.path.isdir(moddir):
        for f in os.listdir(moddir):
            if f.lower().endswith('.archive'):
                deployed[f.lower()] = os.path.join(moddir, f)
    off = [a for a in base_mod['archives'] if os.path.basename(a).lower() not in deployed]
    name = C.clean_mod_name(base_mod.get('name', '')) or base_mod.get('name', '')
    if off:
        warn.append(tr(f'Мод-основа «{name}» сейчас выключен. Включи его в Vortex, иначе в игре будут '
                       'щели у рук, а части тела могут пропасть.',
                       f'The base body mod "{name}" is disabled. Enable it in Vortex, otherwise the arms '
                       'will have gaps and body parts may disappear.'))
    # материалы файлов из мода-основы
    need = {}
    for rel, v in s['files'].items():
        if v.get('src') != 'mod':
            continue
        try:
            need.update(_material_refs(os.path.join(project_dir, 'base', v['json'])))
        except Exception:
            continue
    if not need:
        return warn
    have = set()
    try:
        for d in C.game_archive_dirs(cfg):
            have |= _hashes(C._archives_in(d, 1))
    except Exception:
        return warn
    have |= _hashes(deployed.values())
    have |= _hashes(C._archives_in(os.path.join(game, 'red4ext', 'plugins'), 4))
    if off:   # мод-основу и так попросили включить - его файлы считаем доступными
        have |= _hashes(base_mod['archives'])
    missing = {h: p for h, p in need.items() if h not in have}
    if not missing:
        return warn
    givers = []
    for m in C.scan_mods(cfg, include_ours=False):
        if any(os.path.basename(a).lower() in deployed for a in m['archives']):
            continue
        if _hashes(m['archives']) & set(missing):
            givers.append(C.clean_mod_name(m['name']) or m['name'])
    if givers:
        warn.append(tr('Нужен для кожи тела, но выключен: ', 'Needed for the body skin but disabled: ') +
                    ', '.join(f'«{g}»' for g in givers[:3]) +
                    tr('. Включи его в Vortex, иначе части тела (например ноги) будут невидимы.',
                       '. Enable it in Vortex, otherwise body parts (like the legs) will be invisible.'))
    else:
        ex = next((p for p in missing.values() if p), '')
        warn.append(tr('В игре нет материалов кожи, которые нужны мод-основе', 'The game lacks skin materials the base mod needs') +
                    (f' ({ex})' if ex else '') +
                    tr('. Поставь моды, которые требует его страница на Nexus, иначе части тела будут невидимы.',
                       '. Install the mods its Nexus page requires, otherwise body parts will be invisible.'))
    return warn
