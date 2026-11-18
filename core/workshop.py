# Body Studio - мастерская в Blender: своё тело V из обычного тела или мода тела.
# Запускает лаунчер: blender workshop.blend --python workshop.py -- --project <id проекта>
# Форма тела = основа + ползунки + лепка. Ползунки пересчитывают форму на лету, лепка хранится
# отдельно как сдвиг каждой точки, поэтому одно не стирает другое.
import bpy, os, sys, json, shutil, traceback, subprocess
import numpy as np

CORE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CORE)
import common as C
import bodyset as BS
from meshpos import MeshPos

tr = C.tr
TAB = 'Body Studio'
SKIN = np.array((0.85, 0.66, 0.55, 1.0))
LOCKED = np.array((0.42, 0.47, 0.62, 1.0))   # шея, стык с руками, ступни - их не меняем
ARM = (0.62, 0.58, 0.56, 1.0)
MARK = np.array((0.95, 0.12, 0.12, 1.0))     # проблемные места после проверки
BEFORE = np.array((0.60, 0.64, 0.72, 1.0))   # тело «до» рядом для сравнения
BODY_OBJ = tr('Тело', 'Body')

S = {'project': None, 'pdir': None, 'set': None, 'base': None, 'F': None, 'sculpt': None, 'applied': {},
     'arms': [], 'marked': False}


class UserError(Exception):
    pass


def arg(name):
    a = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else None


def report(op, msg, level='INFO'):
    bpy.context.scene.bs_status = msg
    if op:
        op.report({level}, msg)


class Progress:
    def __init__(self, title):
        self.title = title
        self.wm = bpy.context.window_manager
        self.wm.progress_begin(0, 100)

    def __call__(self, pct, text=''):
        self.wm.progress_update(int(pct))
        try:
            bpy.context.workspace.status_text_set(f'{self.title}: {text} ({int(pct)}%)')
            bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        except Exception:
            pass

    def done(self):
        self.wm.progress_end()
        try:
            bpy.context.workspace.status_text_set(None)
        except Exception:
            pass


# ---------- проект ----------

def project_path(*a):
    return os.path.join(S['pdir'], *a)


def load_project():
    pid = arg('--project')
    if not pid:
        raise RuntimeError(tr('Не выбран проект. Открой мастерскую из лаунчера.', 'No project. Open the workshop from the launcher.'))
    pdir = pid if os.path.isdir(pid) else os.path.join(C.PROJECTS, pid)
    S['pdir'] = pdir
    S['project'] = json.load(open(os.path.join(pdir, 'project.json'), encoding='utf-8'))
    return S['project']


def base_mod():
    path = S['project'].get('base_mod') or ''
    if not path:
        return None
    for m in C.scan_mods(include_ours=False):
        if os.path.normcase(m['path']) == os.path.normcase(path):
            return m
    m = C.manual_mod(path)
    if not m:
        raise RuntimeError(tr('Не найден мод-основа: ', 'Base body mod not found: ') + path)
    return m


def gender():
    """Пол тела проекта: 'female' или 'male'."""
    return 'male' if (S.get('project') or {}).get('gender') == 'male' else 'female'


def base_label():
    p = S['project'] or {}
    return C.clean_mod_name(p.get('base_name') or '') or tr('Обычное тело', 'Vanilla body')


# ---------- форма ----------

def values():
    sc = bpy.context.scene
    return {k: float(getattr(sc, 'bs_' + k)) for k, _, _ in BS.SLIDERS}


def shape(vals):
    """Основа + ползунки (без лепки)."""
    P = S['base'].pos.copy()
    for k, v in vals.items():
        if abs(v) > 1e-6:
            P += S['F'][k] * v
    return P


def body_obj():
    return bpy.data.objects.get(BODY_OBJ)


def read_co(ob):
    a = np.empty(len(ob.data.vertices) * 3, np.float32)
    ob.data.vertices.foreach_get('co', a)
    return a.reshape(-1, 3).astype(np.float64)


def write_co(ob, P):
    ob.data.vertices.foreach_set('co', np.asarray(P, np.float32).ravel())
    ob.data.update()


def sync_sculpt():
    """Запоминает лепку: текущая форма объекта минус основа с ползунками, которые на нём стоят."""
    ob = body_obj()
    if ob is None or S['base'] is None:
        return
    if bpy.context.mode == 'SCULPT':
        bpy.ops.object.mode_set(mode='OBJECT')  # из режима лепки точки попадают в меш только при выходе
    S['sculpt'] = (read_co(ob) - shape(S['applied'])) * (S['base'].lock > 0)[:, None]


def refresh_shape():
    ob = body_obj()
    if ob is None or S['base'] is None:
        return
    sync_sculpt()
    S['applied'] = values()
    write_co(ob, shape(S['applied']) + S['sculpt'])
    if S.get('marked'):   # форма поменялась - старые отметки проверки уже не про неё
        paint_body(None)


def on_symmetry(self, context):
    """Галочка «Симметрия» сразу включает или выключает её на теле."""
    ob = body_obj()
    if ob is not None:
        ob.use_mesh_mirror_x = self.bs_symmetry


def on_slider(self, context):
    if S['base'] is not None and not S.get('loading'):
        refresh_shape()


def final_disp():
    sync_sculpt()
    return shape(values()) + S['sculpt'] - S['base'].pos


# ---------- сцена ----------

def build_object(name, pos, tris, coll, colors):
    me = bpy.data.meshes.new(name)
    me.vertices.add(len(pos))
    me.vertices.foreach_set('co', np.asarray(pos, np.float32).ravel())
    tris = np.asarray(tris, np.int64)
    me.loops.add(len(tris) * 3)
    me.polygons.add(len(tris))
    me.loops.foreach_set('vertex_index', tris.astype(np.int32).ravel())
    me.polygons.foreach_set('loop_start', np.arange(0, len(tris) * 3, 3, dtype=np.int32))
    me.polygons.foreach_set('use_smooth', np.ones(len(tris), bool))
    me.update(calc_edges=True)
    me.validate(clean_customdata=False)
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    attr = me.color_attributes.new('col', 'FLOAT_COLOR', 'POINT')
    attr.data.foreach_set('color', np.asarray(colors, np.float32).ravel())
    me.color_attributes.active_color = attr
    return ob


def body_colors(marks=None):
    """Цвет тела: кожа, синие закрытые зоны, красные отметки проверки."""
    lock = S['base'].lock[:, None]
    col = LOCKED * (1 - lock) + SKIN * lock
    if marks is not None and len(marks):
        col[marks] = MARK
    return col


def paint_body(marks=None):
    ob = body_obj()
    if ob is None or S['base'] is None:
        return
    attr = ob.data.color_attributes.get('col')
    if attr:
        attr.data.foreach_set('color', np.asarray(body_colors(marks), np.float32).ravel())
        ob.data.update()
    S['marked'] = marks is not None and len(marks) > 0


def set_mask(ob, lock):
    """Маска лепки: закрытые зоны (шея, стык с руками, ступни) кисти не трогают."""
    me = ob.data
    a = me.attributes.get('.sculpt_mask') or me.attributes.new('.sculpt_mask', 'FLOAT', 'POINT')
    a.data.foreach_set('value', (1.0 - np.asarray(lock, np.float32)).ravel())


def setup_scene(prog=None):
    if bpy.context.scene.bs_compare:
        S['cmp_off'] = 0
        bpy.context.scene.bs_compare = False
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)
    coll = bpy.data.collections.new(BODY_OBJ)
    bpy.context.scene.collection.children.link(coll)
    s = BS.collect(S['pdir'], base_mod(), prog, gender())
    S['set'] = s
    if prog:
        prog(80, tr('Готовлю ползунки...', 'Preparing sliders...'))
    base = BS.Base(BS.set_json(S['pdir'], s, BS.s_torso(s)), BS.gender_of(s))
    S['base'] = base
    S['F'] = base.fields()
    S['sculpt'] = np.zeros_like(base.pos)   # каждый запуск с нуля, сохранённое - только в профилях
    ob = build_object(BODY_OBJ, base.pos, base.tris, coll, body_colors())
    set_mask(ob, base.lock)
    S['arms'] = []
    S['marked'] = False
    S['mirror'] = None
    for k, r in BS.s_arms(s).items():
        if r in s['files']:
            p, t, _ = MeshPos(BS.set_json(S['pdir'], s, r)).lod0()
            S['arms'].append((np.asarray(p, np.float64), np.asarray(t)))
            a = build_object(tr('Рука', 'Arm') + k[-2:], p, t, coll, np.tile(ARM, (len(p), 1)))
            a.hide_select = True
    # всё на нуле: ползунки, тег, симметрия включена, тонкие настройки скрыты; имя - из лаунчера
    sc = bpy.context.scene
    S['loading'] = True
    try:
        for k, _, _ in BS.SLIDERS:
            setattr(sc, 'bs_' + k, 0.0)
        sc.bs_name = S['project'].get('name', '')
        sc.bs_tag = ''
        sc.bs_symmetry = True
        sc.bs_advanced = False
        prof = list_profiles()   # в списке сразу последний профиль, но сам он не загружается
        try:
            sc.bs_profile = prof[0][0] if prof else NO_PROF
        except Exception:
            pass
    finally:
        S['loading'] = False
    S['applied'] = values()
    write_co(ob, shape(S['applied']) + S['sculpt'])
    ob.use_mesh_mirror_x = sc.bs_symmetry
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    setup_view()
    mark_saved()


def setup_view():
    from mathutils import Euler
    from math import radians
    for area in (a for w in bpy.context.window_manager.windows if w.screen for a in w.screen.areas):
        if area.type != 'VIEW_3D':
            continue
        sp = area.spaces.active
        sp.shading.type = 'SOLID'
        sp.shading.color_type = 'VERTEX'
        try:
            sp.shading.show_cavity = True
        except Exception:
            pass
        r3 = sp.region_3d
        r3.view_perspective = 'PERSP'
        r3.view_rotation = Euler((radians(85), 0, radians(160))).to_quaternion()
        r3.view_location = (0, 0, 0.9)
        r3.view_distance = 3.2
        sp.show_region_ui = True
        if not bpy.app.background:
            tidy_view(sp)
        for region in area.regions:
            if region.type == 'UI':
                try:
                    region.active_panel_category = TAB
                except Exception:
                    pass


# ---------- кнопки ----------

class BODY_OT_reset(bpy.types.Operator):
    bl_idname = 'body.reset'
    bl_label = tr('Сбросить', 'Reset')
    bl_description = tr('Вернуть ползунки (или лепку) к основе', 'Bring sliders (or sculpting) back to the base')
    bl_options = {'REGISTER', 'UNDO'}
    what: bpy.props.StringProperty(default='SLIDERS')

    def execute(self, context):
        if self.what == 'SCULPT':
            sync_sculpt()
            S['sculpt'][:] = 0
            write_co(body_obj(), shape(S['applied']))
            report(self, tr('Лепка сброшена.', 'Sculpting reset.'))
            return {'FINISHED'}
        S['loading'] = True
        try:
            for k, _, _ in BS.SLIDERS:
                setattr(context.scene, 'bs_' + k, 0.0)
        finally:
            S['loading'] = False
        refresh_shape()
        report(self, tr('Ползунки сброшены.', 'Sliders reset.'))
        return {'FINISHED'}


TIPS = {
    'sculpt': (tr('Лепка тела', 'Sculpting the body'), tr(
        'Кисти меняют форму тела вручную. Симметрия включена: что делаешь слева, повторится справа. '
        'Её можно выключить галочкой «Симметрия».\n'
        'Синеватые зоны (шея, стык с руками, ступни) закрыты: там тело соединяется с головой, руками и '
        'обувью, поэтому их менять нельзя, иначе в игре появятся дыры на швах.\n'
        'Работай небольшой силой и короткими мазками. Если что-то пошло не так: Ctrl+Z или «Сбросить лепку».',
        'Brushes change the body shape by hand. Symmetry is on: what you do on the left repeats on the right. '
        'You can turn it off with the "Symmetry" checkbox.\n'
        'Bluish zones (neck, arm joins, feet) are locked: that is where the body connects to the head, arms and '
        'shoes, so they cannot change, otherwise holes would appear at the seams in the game.\n'
        'Use low strength and short strokes. If something goes wrong: Ctrl+Z or "Reset sculpting".')),
}


def tip_hidden(key):
    return key in C.load_config().get('hidden_tips', [])


def hide_tip(key):
    cfg = C.load_config()
    cfg['hidden_tips'] = sorted(set(cfg.get('hidden_tips', [])) | {key})
    C.save_config(cfg)


def draw_tip(lay, key, op):
    import textwrap
    col = lay.column(align=True)
    for i, par in enumerate(TIPS[key][1].split('\n')):
        if i:
            col.separator()
        for j, line in enumerate(textwrap.wrap(par, 78)):
            col.label(text=line, icon=('INFO' if i == 0 and j == 0 else 'NONE'))
    lay.separator()
    lay.prop(op, 'dont_show')


BRUSHES = (('Grab', tr('Тянуть', 'Grab'), tr('Тянуть форму мышкой', 'Pull the shape with the mouse')),
           ('Inflate/Deflate', tr('Надуть', 'Inflate'), tr('Сделать объёмнее (с Ctrl - сдуть)', 'Add volume (Ctrl - remove)')),
           ('Smooth', tr('Сгладить', 'Smooth'), tr('Сгладить бугры и вмятины', 'Smooth out bumps and dents')),
           ('Draw', tr('Нарастить', 'Draw'), tr('Нарастить поверхность (с Ctrl - вдавить)', 'Build up the surface (Ctrl - dig in)')))


def current_brush():
    try:
        b = bpy.context.tool_settings.sculpt.brush
        return b.name.split('.')[0] if b else ''
    except Exception:
        return ''


def _activate(name):
    for call in (lambda: bpy.ops.brush.asset_activate(
                     asset_library_type='ESSENTIALS',
                     relative_asset_identifier='brushes\\essentials_brushes-mesh_sculpt.blend\\Brush\\' + name),
                 lambda: bpy.ops.wm.tool_set_by_id(name='builtin_brush.' + name.split('/')[0])):
        try:
            call()
            return True
        except Exception:
            pass
    return False


def activate_brush(name):
    _activate(name)
    if bpy.app.background:
        return
    tries = [0]

    def retry():
        tries[0] += 1
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type == 'VIEW_3D':
                    tidy_view(a.spaces.active)
                    a.tag_redraw()
        if bpy.context.mode != 'SCULPT' or current_brush() == name or tries[0] > 15:
            return None
        win = bpy.context.window_manager.windows[0]
        area = next((a for a in win.screen.areas if a.type == 'VIEW_3D'), None)
        if area:
            with bpy.context.temp_override(window=win, area=area):
                _activate(name)
        return 0.3
    bpy.app.timers.register(retry, first_interval=0.2)


class BODY_OT_sculpt(bpy.types.Operator):
    bl_idname = 'body.sculpt'
    bl_label = tr('Лепить', 'Sculpt')
    bl_description = tr('Режим лепки кистями (симметрия включена)', 'Sculpt mode with brushes (symmetry on)')
    dont_show: bpy.props.BoolProperty(name=tr('Больше не показывать', "Don't show again"), default=False,
                                      options={'SKIP_SAVE'})

    def invoke(self, context, event):
        if tip_hidden('sculpt') or bpy.app.background:
            return self.execute(context)
        try:
            w = context.window
            w.cursor_warp(w.width // 2, w.height // 2 + 120)
        except Exception:
            pass
        return context.window_manager.invoke_props_dialog(self, width=520, title=TIPS['sculpt'][0],
                                                          confirm_text=tr('Понятно, лепить', 'Got it, sculpt'))

    def draw(self, context):
        draw_tip(self.layout, 'sculpt', self)

    def execute(self, context):
        if self.dont_show:
            hide_tip('sculpt')
        ob = body_obj()
        sync_sculpt()
        context.view_layer.objects.active = ob
        ob.select_set(True)
        bpy.ops.object.mode_set(mode='SCULPT')
        ob.use_mesh_mirror_x = context.scene.bs_symmetry
        activate_brush('Grab')
        if context.scene.bs_tab != 'SCULPT':
            context.scene.bs_tab = 'SCULPT'
        report(self, tr('Лепка: F - размер кисти, Shift+F - сила. Синие зоны закрыты.',
                        'Sculpt: F - brush size, Shift+F - strength. Blue zones are locked.'))
        return {'FINISHED'}


class BODY_OT_brush(bpy.types.Operator):
    bl_idname = 'body.brush'
    bl_label = tr('Кисть', 'Brush')
    bl_description = tr('Выбрать кисть', 'Choose a brush')
    name: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, props):
        return next((d for n, _, d in BRUSHES if n == props.name), cls.bl_description)

    def execute(self, context):
        if context.mode != 'SCULPT':
            bpy.ops.body.sculpt('EXEC_DEFAULT')
        activate_brush(self.name)
        return {'FINISHED'}


class BODY_OT_sculpt_done(bpy.types.Operator):
    bl_idname = 'body.sculpt_done'
    bl_label = tr('Готово', 'Done')
    bl_description = tr('Закончить лепку', 'Finish sculpting')

    def execute(self, context):
        sync_sculpt()
        context.scene.bs_tab = 'SLIDERS'
        report(self, tr('Лепка сохранена в форме тела. Ползунки можно двигать дальше.',
                        'Sculpting is kept in the body shape. You can keep moving the sliders.'))
        return {'FINISHED'}


# ---------- до и после ----------

CMP_COLL = 'BS_compare'


def compare_off():
    coll = bpy.data.collections.get(CMP_COLL)
    if coll is None:
        return
    for ob in list(coll.objects):
        data, own = ob.data, ob.get('bs_own')
        bpy.data.objects.remove(ob, do_unlink=True)
        if own and data is not None and data.users == 0:
            (bpy.data.meshes if ob_type_mesh(data) else bpy.data.curves).remove(data)
    bpy.data.collections.remove(coll)


def ob_type_mesh(data):
    return isinstance(data, bpy.types.Mesh)


def label_obj(coll, text, x, z):
    cu = bpy.data.curves.new('bs_label', 'FONT')
    cu.body = text
    cu.align_x = 'CENTER'
    cu.size = 0.11
    ob = bpy.data.objects.new('bs_label', cu)
    coll.objects.link(ob)
    from math import radians
    ob.location = (x, 0, z)
    ob.rotation_euler = (radians(90), 0, radians(180))   # лицом к камере мастерской
    ob['bs_own'] = True
    ob.hide_select = True
    return ob


def compare_on():
    compare_off()
    base = S['base']
    meshes = [o for o in bpy.data.objects if o.type == 'MESH']
    half = max(float(np.abs(read_co(o)[:, 0]).max()) for o in meshes)
    off = 2 * half + 0.12   # «до» стоит слева от «после», не задевая руки
    coll = bpy.data.collections.new(CMP_COLL)
    bpy.context.scene.collection.children.link(coll)
    b = build_object(tr('До', 'Before'), base.pos, base.tris, coll, np.tile(BEFORE, (base.n, 1)))
    b['bs_own'] = True
    for o in meshes:
        if o.name == BODY_OBJ:
            b.location.x = off
            continue
        a = bpy.data.objects.new(o.name + ' ' + tr('до', 'before'), o.data)   # та же рука, только сдвинута
        coll.objects.link(a)
        a.location.x = off
        a.hide_select = True
    b.hide_select = True
    top = float(base.pos[:, 2].max()) + 0.12
    label_obj(coll, tr('ДО', 'BEFORE'), off, top)
    label_obj(coll, tr('ПОСЛЕ', 'AFTER'), 0, top)
    return off


def on_compare(self, context):
    try:
        if self.bs_compare:
            off = compare_on()
            S['cmp_off'] = off
            shift = off / 2
        else:
            compare_off()
            shift = -S.get('cmp_off', 0) / 2
        for area in (a for w in context.window_manager.windows if w.screen for a in w.screen.areas):
            if area.type == 'VIEW_3D':
                r3 = area.spaces.active.region_3d
                r3.view_location.x += shift
                r3.view_distance *= 1.35 if self.bs_compare else 1 / 1.35
    except Exception:
        C.log('compare:\n' + traceback.format_exc())


# ---------- пресеты ----------

PRESETS_FILE = os.path.join(C.DATA, 'presets.json')
BUILTIN_F = {
    tr('Стройная', 'Slim'): {'weight': -0.5, 'waist': -0.3, 'belly': -0.5, 'hips': -0.3, 'butt': -0.2,
                             'thighs': -0.4, 'calves': -0.3, 'breast_size': -0.2},
    tr('Спортивная', 'Athletic'): {'weight': -0.3, 'waist': -0.3, 'belly': -0.7, 'flanks': -0.3, 'butt': 0.4,
                                   'butt_lift': 0.6, 'thighs': 0.3, 'calves': 0.4, 'breast_size': -0.2},
    tr('Песочные часы', 'Hourglass'): {'waist': -0.8, 'flanks': -0.4, 'hips': 0.6, 'butt': 0.4,
                                       'breast_size': 0.5, 'thighs': 0.2},
    tr('Пышная', 'Curvy'): {'weight': 0.5, 'breast_size': 0.7, 'hips': 0.7, 'butt': 0.7, 'thighs': 0.6,
                            'belly': 0.2, 'calves': 0.3},
}
BUILTIN_M = {
    tr('Стройный', 'Lean'): {'weight': -0.5, 'waist': -0.3, 'belly': -0.6, 'flanks': -0.3, 'thighs': -0.3,
                             'calves': -0.2, 'breast_size': -0.3},
    tr('Атлет', 'Athletic'): {'weight': -0.2, 'breast_size': 0.6, 'breast_width': 0.4, 'ribcage': 0.4, 'waist': -0.4,
                              'belly': -0.7, 'flanks': -0.4, 'butt': 0.3, 'thighs': 0.5, 'calves': 0.4},
    tr('Качок', 'Bodybuilder'): {'breast_size': 1.0, 'breast_project': 0.6, 'breast_width': 0.6, 'ribcage': 0.8,
                                 'waist': -0.3, 'belly': -0.5, 'butt': 0.5, 'thighs': 0.9, 'thigh_outer': 0.4,
                                 'calves': 0.8},
    tr('Плотный', 'Heavy'): {'weight': 0.7, 'belly': 0.8, 'belly_lower': 0.4, 'waist': 0.6, 'flanks': 0.5,
                             'breast_size': 0.3, 'thighs': 0.4, 'butt': 0.3},
}


def builtin():
    return BUILTIN_M if gender() == 'male' else BUILTIN_F


_preset_items = []   # Blender требует держать строки списка живыми


def user_presets():
    try:
        d = json.load(open(PRESETS_FILE, encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_user_presets(d):
    os.makedirs(C.DATA, exist_ok=True)
    tmp = PRESETS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PRESETS_FILE)


def preset_items(self, context):
    items = [('b:' + n, n, tr('Готовый пресет', 'Built-in preset'), 'PRESET', i) for i, n in enumerate(builtin())]
    for i, n in enumerate(sorted(user_presets())):
        items.append(('u:' + n, n, tr('Твой пресет', 'Your preset'), 'USER', 100 + i))
    _preset_items[:] = items
    return _preset_items


def preset_values(ident):
    kind, name = ident[:2], ident[2:]
    return builtin().get(name) if kind == 'b:' else user_presets().get(name)


class BODY_OT_preset_apply(bpy.types.Operator):
    bl_idname = 'body.preset_apply'
    bl_label = tr('Применить', 'Apply')
    bl_description = tr('Поставить ползунки как в пресете (лепка остаётся)', 'Set the sliders from the preset (sculpting stays)')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene
        vals = preset_values(sc.bs_preset or '')
        if not vals:
            raise UserError(tr('Выбери пресет.', 'Choose a preset.'))
        S['loading'] = True
        try:
            for k, _, _ in BS.SLIDERS:   # скрытые для этого тела ползунки (грудь у мужского V) - в ноль
                v = vals.get(k, 0.0) if BS.slider_visible(k, gender()) else 0.0
                setattr(sc, 'bs_' + k, float(v))
        finally:
            S['loading'] = False
        refresh_shape()
        fine = {k for keys in advanced().values() for k in keys}
        if any(abs(vals.get(k, 0.0)) > 1e-6 for k in fine):
            sc.bs_advanced = True   # иначе часть изменений не видно на панели
        report(self, tr('Пресет «', 'Preset "') + sc.bs_preset[2:] + tr('» применён.', '" applied.'))
        return {'FINISHED'}


class BODY_OT_preset_save(bpy.types.Operator):
    bl_idname = 'body.preset_save'
    bl_label = tr('Сохранить пресет', 'Save preset')
    bl_description = tr('Запомнить ползунки под своим именем, чтобы ставить их на любое тело',
                        'Remember the sliders under a name to use them on any body')
    name: bpy.props.StringProperty(name=tr('Имя', 'Name'), options={'SKIP_SAVE'})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320, title=self.bl_label,
                                                          confirm_text=tr('Сохранить', 'Save'))

    def execute(self, context):
        name = self.name.strip()
        if not name:
            raise UserError(tr('Напиши имя пресета.', 'Type a preset name.'))
        vals = {k: round(v, 3) for k, v in values().items() if abs(v) > 1e-4}
        if not vals:
            raise UserError(tr('Все ползунки на нуле, сохранять нечего.', 'All sliders are at zero, nothing to save.'))
        d = user_presets()
        d[name] = vals
        save_user_presets(d)
        context.scene.bs_preset = 'u:' + name
        report(self, tr('Пресет «', 'Preset "') + name + tr('» сохранён. Он есть во всех телах.', '" saved. It is available in every body.'))
        return {'FINISHED'}


class BODY_OT_preset_delete(bpy.types.Operator):
    bl_idname = 'body.preset_delete'
    bl_label = tr('Удалить пресет', 'Delete preset')
    bl_description = tr('Удалить свой пресет (готовые удалить нельзя)', 'Delete your preset (built-in ones stay)')

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        ident = context.scene.bs_preset or ''
        if not ident.startswith('u:'):
            raise UserError(tr('Готовые пресеты удалить нельзя.', 'Built-in presets cannot be deleted.'))
        d = user_presets()
        d.pop(ident[2:], None)
        save_user_presets(d)
        context.scene.bs_preset = 'b:' + next(iter(builtin()))
        report(self, tr('Пресет удалён.', 'Preset deleted.'))
        return {'FINISHED'}


# ---------- профили тела ----------
# Мастерская всегда открывает тело с нуля. Всё, что нужно сохранить (ползунки, лепку, имя, тег,
# симметрию, тонкие настройки), сохраняется профилем: <проект>\profiles\<id>\profile.json + sculpt.npy.

_profile_items = []
NO_PROF = 'none'   # пункт списка, когда профилей ещё нет


def chosen_profile(sc):
    v = sc.bs_profile or ''
    return '' if v == NO_PROF else v


def profiles_dir():
    return project_path('profiles')


def list_profiles():
    """[(id, данные)] новые сверху."""
    out = []
    if not S.get('pdir'):
        return out
    d = profiles_dir()
    if not os.path.isdir(d):
        return out
    for pid in os.listdir(d):
        try:
            out.append((pid, json.load(open(os.path.join(d, pid, 'profile.json'), encoding='utf-8'))))
        except Exception:
            pass
    return sorted(out, key=lambda x: x[1].get('saved', x[0]), reverse=True)


def profile_items(self, context):
    items = []
    for i, (pid, v) in enumerate(list_profiles()):
        items.append((pid, f"{v.get('name', pid)}  ·  {v.get('when', '')}",
                      tr('Сохранённый профиль тела', 'A saved body profile'), 'FILE_BACKUP', i))
    if not items:
        items = [(NO_PROF, tr('пока нет профилей', 'no profiles yet'), '', 'BLANK1', 0)]
    _profile_items[:] = items
    return _profile_items


def default_profile_name():
    cur = chosen_profile(bpy.context.scene)
    for pid, v in list_profiles():
        if pid == cur:
            return v.get('name', '')
    return tr('Профиль ', 'Profile ') + str(len(list_profiles()) + 1)


class BODY_OT_profile_save(bpy.types.Operator):
    bl_idname = 'body.profile_save'
    bl_label = tr('Сохранить профиль', 'Save profile')
    bl_description = tr('Сохранить всё как есть: ползунки, лепку, имя, тег, симметрию. '
                        'Без профиля мастерская при следующем запуске начнёт с нуля',
                        'Save everything as it is: sliders, sculpting, name, tag, symmetry. '
                        'Without a profile the workshop starts from zero next time')
    name: bpy.props.StringProperty(name=tr('Имя', 'Name'), options={'SKIP_SAVE'})

    def invoke(self, context, event):
        self.name = default_profile_name()
        return context.window_manager.invoke_props_dialog(self, width=340, title=self.bl_label,
                                                          confirm_text=tr('Сохранить', 'Save'))

    def draw(self, context):
        self.layout.prop(self, 'name')
        self.layout.label(text=tr('Такое же имя - профиль обновится.', 'Same name - the profile is updated.'), icon='INFO')

    def execute(self, context):
        import datetime
        sc = context.scene
        name = self.name.strip() or tr('Профиль ', 'Profile ') + str(len(list_profiles()) + 1)
        was = context.mode == 'SCULPT'
        sync_sculpt()
        now = datetime.datetime.now()
        same = next((pid for pid, v in list_profiles() if v.get('name') == name), None)
        pid = same or now.strftime('%Y%m%d_%H%M%S_%f')
        d = os.path.join(profiles_dir(), pid)
        os.makedirs(d, exist_ok=True)
        np.save(os.path.join(d, 'sculpt.npy'), S['sculpt'].astype(np.float32))
        v = {'name': name, 'when': now.strftime('%d.%m %H:%M'), 'saved': now.isoformat(timespec='microseconds'),
             'values': {k: round(float(x), 4) for k, x in values().items()},
             'body_name': sc.bs_name, 'tag': sc.bs_tag, 'symmetry': bool(sc.bs_symmetry), 'advanced': bool(sc.bs_advanced)}
        tmp = os.path.join(d, 'profile.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(v, f, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(d, 'profile.json'))
        try:   # в лаунчере дата «Изменено» берётся у project.json
            os.utime(project_path('project.json'))
        except Exception:
            pass
        sc.bs_profile = pid
        mark_saved()
        if was:
            bpy.ops.object.mode_set(mode='SCULPT')
        report(self, tr(f'Профиль «{name}» ' + ('обновлён.' if same else 'сохранён.'),
                        f'Profile "{name}" ' + ('updated.' if same else 'saved.')))
        return {'FINISHED'}


class BODY_OT_profile_load(bpy.types.Operator):
    bl_idname = 'body.profile_load'
    bl_label = tr('Загрузить профиль', 'Load profile')
    bl_description = tr('Поставить всё как в выбранном профиле («Отменить» вернёт как было)',
                        'Set everything as in the chosen profile (Undo brings it back)')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene
        pid = chosen_profile(sc)
        d = os.path.join(profiles_dir(), pid)
        if not pid or not os.path.isfile(os.path.join(d, 'profile.json')):
            raise UserError(tr('Выбери профиль.', 'Choose a profile.'))
        v = json.load(open(os.path.join(d, 'profile.json'), encoding='utf-8'))
        vals = v.get('values', {})
        was = context.mode == 'SCULPT'
        sync_sculpt()
        note = ''
        sculpt = np.zeros_like(S['base'].pos)
        f = os.path.join(d, 'sculpt.npy')
        if os.path.isfile(f):
            a = np.load(f).astype(np.float64)
            if a.shape == sculpt.shape:
                sculpt = a * (S['base'].lock > 0)[:, None]
            else:
                note = tr(' Лепку из него вернуть нельзя: основа тела с тех пор поменялась.',
                          ' Its sculpting cannot come back: the base body changed since then.')
        S['loading'] = True
        try:
            for k, _, _ in BS.SLIDERS:
                setattr(sc, 'bs_' + k, float(vals.get(k, 0.0)))
            sc.bs_name = v.get('body_name') or S['project'].get('name', '')
            sc.bs_tag = v.get('tag', '')
            sc.bs_symmetry = bool(v.get('symmetry', True))
        finally:
            S['loading'] = False
        S['sculpt'] = sculpt
        S['applied'] = values()
        ob = body_obj()
        write_co(ob, shape(S['applied']) + S['sculpt'])
        ob.use_mesh_mirror_x = sc.bs_symmetry
        if S.get('marked'):
            paint_body(None)
        fine = {k for keys in advanced().values() for k in keys}
        sc.bs_advanced = bool(v.get('advanced')) or any(abs(vals.get(k, 0.0)) > 1e-6 for k in fine)
        mark_saved()
        if was:
            bpy.ops.object.mode_set(mode='SCULPT')
        name = v.get('name', pid)
        report(self, tr(f'Профиль «{name}» загружен.', f'Profile "{name}" loaded.') + note)
        return {'FINISHED'}


class BODY_OT_profile_delete(bpy.types.Operator):
    bl_idname = 'body.profile_delete'
    bl_label = tr('Удалить профиль', 'Delete profile')
    bl_description = tr('Удалить выбранный профиль (само тело на экране не меняется)',
                        'Delete the chosen profile (the body on screen stays)')

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        pid = chosen_profile(context.scene)
        d = os.path.join(profiles_dir(), pid)
        if not pid or not os.path.isdir(d):
            raise UserError(tr('Выбери профиль.', 'Choose a profile.'))
        C.rmtree(d)
        rest = list_profiles()
        if rest:
            context.scene.bs_profile = rest[0][0]
        report(self, tr('Профиль удалён.', 'Profile deleted.'))
        return {'FINISHED'}


# ---------- выровнять лепку ----------

def mirror_map():
    """Для каждой точки - её пара с другой стороны тела (по основе)."""
    if S.get('mirror') is None or len(S['mirror']) != S['base'].n:
        from mathutils import kdtree
        P = S['base'].pos
        kd = kdtree.KDTree(len(P))
        for i, p in enumerate(P):
            kd.insert(p, i)
        kd.balance()
        S['mirror'] = np.array([kd.find((-p[0], p[1], p[2]))[1] for p in P], np.int64)
    return S['mirror']


class BODY_OT_mirror(bpy.types.Operator):
    bl_idname = 'body.mirror'
    bl_label = tr('Выровнять лепку', 'Mirror sculpting')
    bl_description = tr('Сделать одну сторону тела как другую: полезно, если лепил без симметрии',
                        'Make one side of the body like the other: handy if you sculpted without symmetry')
    bl_options = {'REGISTER', 'UNDO'}
    side: bpy.props.EnumProperty(items=[('LEFT', 'left', ''), ('RIGHT', 'right', '')], default='LEFT')

    @classmethod
    def description(cls, context, props):
        if props.side == 'LEFT':
            return tr('Левую сторону тела скопировать на правую (лево и право самого тела, как у V)',
                      "Copy the body's left side onto the right (V's own left and right)")
        return tr('Правую сторону тела скопировать на левую (лево и право самого тела, как у V)',
                  "Copy the body's right side onto the left (V's own left and right)")

    def execute(self, context):
        was = context.mode == 'SCULPT'
        sync_sculpt()
        m = mirror_map()
        P = S['base'].pos
        d = S['sculpt'].copy()
        src = d[m] * np.array((-1.0, 1.0, 1.0))
        # тело смотрит на +Y, значит левая сторона V - это -X
        target = P[:, 0] > 1e-4 if self.side == 'LEFT' else P[:, 0] < -1e-4
        d[target] = src[target]
        d[np.abs(P[:, 0]) <= 1e-4, 0] = 0.0   # середина тела не уезжает вбок
        d *= (S['base'].lock > 0)[:, None]
        S['sculpt'] = d
        write_co(body_obj(), shape(S['applied']) + S['sculpt'])
        if was:
            bpy.ops.object.mode_set(mode='SCULPT')
        report(self, tr('Готово: обе стороны теперь одинаковые.', 'Done: both sides are the same now.'))
        return {'FINISHED'}


# ---------- проверка ----------

def shape_problems():
    """(строки для человека, точки для отметки)."""
    base = S['base']
    found = BS.check_shape(base, final_disp(), S.get('arms') or ())
    msgs = BS.check_messages(base, found)
    marks = np.unique(np.concatenate([v for v in found.values() if len(v)] or [np.zeros(0, np.int64)]))
    if len(marks):   # чуть шире, чтобы было видно
        e = base.edges
        hit = np.zeros(base.n, bool)
        hit[marks] = True
        ring = hit.copy()
        ring[e[hit[e[:, 0]], 1]] = True
        ring[e[hit[e[:, 1]], 0]] = True
        marks = np.where(ring)[0]
    return msgs, marks


class BODY_OT_check(bpy.types.Operator):
    bl_idname = 'body.check'
    bl_label = tr('Проверить тело', 'Check the body')
    bl_description = tr('Найти то, что в игре будет выглядеть плохо: вывернутые места, резкие бугры, '
                        'заход в руки, слишком сильные изменения. Проблемные места станут красными',
                        'Find what will look bad in the game: flipped spots, sharp bumps, clipping into the arms, '
                        'very large changes. Problem spots turn red')

    def execute(self, context):
        if context.mode == 'SCULPT':
            sync_sculpt()
        msgs, marks = shape_problems()
        paint_body(marks)
        if msgs:
            report(self, tr('Нашлось: ', 'Found: ') + '; '.join(msgs) + tr('. Красным отмечено, где.', '. Red shows where.'),
                   'WARNING')
            warn_popup(msgs, tr('Проверка тела', 'Body check'))
        else:
            report(self, tr('Проверка: всё хорошо, проблем не нашлось.', 'Check: all good, no problems found.'))
        return {'FINISHED'}


class BODY_OT_unmark(bpy.types.Operator):
    bl_idname = 'body.unmark'
    bl_label = tr('Убрать отметки', 'Clear marks')
    bl_description = tr('Убрать красные отметки проверки', 'Remove the red check marks')

    def execute(self, context):
        paint_body(None)
        report(self, tr('Отметки убраны.', 'Marks cleared.'))
        return {'FINISHED'}


# ---------- экспорт ----------

def export_name():
    sc = bpy.context.scene
    return sc.bs_name.strip() or S['project'].get('name') or 'My body'


def make_mod(prog):
    """Собирает zip мода тела из текущей формы."""
    d = final_disp()
    if np.abs(d).max() < 1e-5:
        raise UserError(tr('Тело пока не отличается от основы: подвигай ползунки или полепи.',
                           'The body is still the same as the base: move some sliders or sculpt.'))
    tag = bpy.context.scene.bs_tag.strip()
    return BS.build_mod(S['pdir'], S['set'], d, export_name(), tag=tag, prog=prog, base=S['base'])


def deploy_warnings():
    """Предупреждения, если в игре выключен мод-основа или то, что ему нужно (пусто - всё хорошо)."""
    try:
        return BS.check_deployed(S['pdir'], S['set'], base_mod())
    except Exception:
        C.log('deploy check:\n' + traceback.format_exc())
        return []


def warn_popup(lines, title=None):
    """Окно с предупреждением (в фоновом режиме тестов - только в лог)."""
    C.log('WARN: ' + ' | '.join(lines))
    if bpy.app.background or not lines:
        return
    import textwrap
    rows = []
    for i, line in enumerate(lines):   # переносим строки, окно Blender само не переносит
        if i:
            rows.append('')
        rows += textwrap.wrap(line, 70) or ['']

    def draw(self, context):
        for r in rows:
            self.layout.label(text=r)
    try:
        wm = bpy.context.window_manager
        win = bpy.context.window or wm.windows[0]
        with bpy.context.temp_override(window=win):
            wm.popup_menu(draw, title=title or tr('Проверь моды в Vortex', 'Check your mods in Vortex'), icon='ERROR')
    except Exception:
        C.log('warn popup:\n' + traceback.format_exc())


def finish_export(op, ok_msg, extra=()):
    """Итог экспорта: если в игре чего-то не хватает или форма с проблемами - предупреждение вместо «Готово»."""
    w = deploy_warnings() + list(extra)
    try:
        msgs, marks = shape_problems()
        if msgs:
            paint_body(marks)
            w = w + [tr('Проверка тела нашла: ', 'The body check found: ') + '; '.join(msgs) +
                     tr(' (красным на теле).', ' (red on the body).')]
    except Exception:
        C.log('shape check:\n' + traceback.format_exc())
    if w:
        report(op, ok_msg + ' ' + tr('Но: ', 'But: ') + ' '.join(w), 'WARNING')
        warn_popup(w, tr('Мод готов, но обрати внимание', 'The mod is ready, but note'))
    else:
        report(op, ok_msg)


def run_export(op, title, fn):
    prog = Progress(title)
    try:
        return fn(prog)
    finally:
        prog.done()


def profile_pack(arch, root):
    """Папка-мод для профиля Refit Studio: наше тело + руки и ступни основы (если их нет в нашем архиве),
    + .xl с тегом. Только для профиля, в мод для игры чужие файлы не кладём."""
    pdir = project_path('refit_mod')
    C.rmtree(pdir)
    os.makedirs(pdir)
    shutil.copyfile(arch, os.path.join(pdir, os.path.basename(arch)))
    mod = base_mod()
    tag = bpy.context.scene.bs_tag.strip() or (C.body_tag(mod) if mod else '')
    if mod:
        need = {k: p for k, p in C.body_files(gender()).items()
                if not os.path.isfile(C.lp(os.path.join(root, p)))}
        if need:
            tmp = C.work_dir('arms')
            got = C.extract(mod['archives'], tmp, need)
            if got:
                helper = os.path.join(tmp, 'pack', 'zz_base_parts')
                for k, f in got.items():
                    dst = os.path.join(helper, need[k])
                    os.makedirs(C.lp(os.path.dirname(dst)), exist_ok=True)
                    shutil.copyfile(C.lp(f), C.lp(dst))
                C.wk('pack', helper, '-o', os.path.join(tmp, 'pack'))
                shutil.copyfile(helper + '.archive', os.path.join(pdir, 'zz_base_parts.archive'))
            C.rmtree(tmp)
    if tag:
        with open(os.path.join(pdir, 'body.xl'), 'w', encoding='utf-8') as f:
            f.write(f'player:\n  bodyTypes: [ {tag} ]\n')
    return pdir


def refit_installed():
    """Refit Studio стоит на этом компе (есть её папка данных)."""
    return os.path.isdir(C.REFIT_DATA)


def add_to_refit(prog, arch, root):
    """Тело в список тел Refit Studio. Возвращает предупреждение или None. Без Refit Studio ничего не делает."""
    if not refit_installed():
        return None
    try:
        prog(92, tr('тело для Refit Studio', 'body for Refit Studio'))
        pdir = profile_pack(arch, root)
        r = subprocess.run([bpy.app.binary_path, '-b', '--factory-startup', '--python',
                            os.path.join(CORE, 'refit_profile.py'), '--', '--mod', pdir,
                            '--name', export_name(), '--gender', gender()],
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if any(l.startswith('@@OK') for l in r.stdout.splitlines()):
            S['in_refit'] = True
            return None
        err = [l[4:] for l in r.stdout.splitlines() if l.startswith('@@E')]
        C.log('refit profile:\n' + r.stdout[-3000:] + r.stderr[-2000:])
        reason = err[0] if err else tr('профиль не собрался', 'the profile was not built')
    except Exception as e:
        C.log('refit profile:\n' + traceback.format_exc())
        reason = str(e)[:200]
    return tr('В Refit Studio тело не добавилось: ', 'The body was not added to Refit Studio: ') + reason


def export_all(prog):
    """Мод тела + сразу это же тело в Refit Studio. (zip, предупреждения)."""
    S['in_refit'] = False
    zp, arch, root = make_mod(prog)
    w = add_to_refit(prog, arch, root)
    return zp, [w] if w else []


def refit_note():
    if not S.get('in_refit'):
        return ''
    return tr(' Тело уже есть и в Refit Studio (перезапусти её, если открыта).',
              ' The body is also in Refit Studio now (restart it if it is open).')


class BODY_OT_export_vortex(bpy.types.Operator):
    bl_idname = 'body.export_vortex'
    bl_label = tr('В Vortex', 'To Vortex')
    bl_description = tr('Собрать мод тела и сразу установить его через Vortex. Тело само появится и в Refit Studio',
                        'Build the body mod and install it with Vortex. The body also shows up in Refit Studio')

    def execute(self, context):
        vortex = C.find_vortex()
        if not vortex:
            raise UserError(tr('Vortex не найден. Сохрани zip и установи его вручную.', 'Vortex not found. Save a zip and install it by hand.'))
        zp, w = run_export(self, tr('Экспорт', 'Export'), export_all)
        C.send_to_vortex(vortex, zp)
        finish_export(self, tr('Готово! Мод отправлен в Vortex. Включи его там (Enable) и разверни (Deploy).',
                               'Done! The mod was sent to Vortex. Enable it there and Deploy.') + refit_note(), w)
        return {'FINISHED'}


class BODY_OT_export_zip(bpy.types.Operator):
    bl_idname = 'body.export_zip'
    bl_label = tr('Zip для Nexus…', 'Zip for Nexus…')
    bl_description = tr('Собрать мод тела в zip: его можно выложить на Nexus или поставить вручную. '
                        'Тело само появится и в Refit Studio',
                        'Build the body mod as a zip to upload to Nexus or install by hand. '
                        'The body also shows up in Refit Studio')
    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        self.directory = C.output_dir() + os.sep
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        zp, w = run_export(self, tr('Экспорт', 'Export'), export_all)
        dst = os.path.join(self.directory or C.output_dir(), os.path.basename(zp))
        if os.path.abspath(dst) != os.path.abspath(zp):
            shutil.copyfile(zp, dst)
        finish_export(self, tr('Сохранено: ', 'Saved: ') + dst + refit_note(), w)
        return {'FINISHED'}


# ---------- картинка для Nexus ----------

SHOT_H = 1400                     # высота картинки
SHOT_BG = ((0.11, 0.11, 0.15), (0.04, 0.04, 0.06))   # фон сверху и снизу (sRGB)
SHOT_VIEWS = (('front', (90, 0, 180), 0), ('side', (90, 0, 90), 1), ('back', (90, 0, 0), 0))


def shot_path():
    return os.path.join(C.output_dir(), C.safe(export_name(), 60) + '_preview.png')


def render_preview(path):
    """Тело спереди, сбоку и сзади на ровном фоне - одна картинка PNG."""
    from math import radians
    coll = bpy.data.collections.get(BODY_OBJ)
    obs = [o for o in coll.objects if o.type == 'MESH'] if coll else []
    if not obs:
        raise UserError(tr('Тело ещё не загружено.', 'The body is not loaded yet.'))
    sync_sculpt()
    P = np.concatenate([read_co(o) for o in obs])
    lo, hi = P.min(0), P.max(0)
    mid = (lo + hi) / 2
    ppm = SHOT_H * 0.88 / (hi[2] - lo[2])          # точек на метр, одинаково для всех видов
    pad = int(SHOT_H * 0.05)
    sc = bpy.data.scenes.new('BS_shot')
    tmp = C.work_dir('shot')
    cam = None
    panels = []
    try:
        sc.collection.children.link(coll)
        r = sc.render
        r.engine = 'BLENDER_WORKBENCH'
        r.film_transparent = True
        r.resolution_percentage = 100
        r.image_settings.file_format = 'PNG'
        r.image_settings.color_mode = 'RGBA'
        sc.view_settings.view_transform = 'Standard'
        sc.display.render_aa = '16'
        sh = sc.display.shading
        sh.light = 'STUDIO'
        sh.color_type = 'SINGLE'
        sh.single_color = SKIN[:3]
        sh.show_cavity = True
        sh.cavity_type = 'BOTH'
        sh.show_object_outline = False
        cam = bpy.data.objects.new('BS_cam', bpy.data.cameras.new('BS_cam'))
        sc.collection.objects.link(cam)
        sc.camera = cam
        cam.data.type = 'ORTHO'
        for name, rot, axis in SHOT_VIEWS:
            w = int((hi[axis] - lo[axis]) * ppm) + 2 * pad
            r.resolution_x, r.resolution_y = w, SHOT_H
            cam.data.ortho_scale = max(w, SHOT_H) / ppm
            cam.rotation_euler = tuple(radians(a) for a in rot)
            d = np.array((0.0, 0.0, 0.0))
            if axis == 0:
                d[1] = 5.0 if rot[2] == 180 else -5.0
            else:
                d[0] = 5.0
            cam.location = (mid[0] + d[0], mid[1] + d[1], mid[2])
            cam.data.clip_end = 20
            f = os.path.join(tmp, name + '.png')
            r.filepath = f
            bpy.ops.render.render(write_still=True, scene=sc.name)
            img = bpy.data.images.load(f)
            a = np.empty(img.size[0] * img.size[1] * 4, np.float32)
            img.pixels.foreach_get(a)
            panels.append(a.reshape(img.size[1], img.size[0], 4))
            bpy.data.images.remove(img)
    finally:
        if cam:
            data = cam.data
            bpy.data.objects.remove(cam, do_unlink=True)
            bpy.data.cameras.remove(data)
        bpy.data.scenes.remove(sc)
        C.rmtree(tmp)
    gap = pad
    W = sum(p.shape[1] for p in panels) + gap * (len(panels) - 1)
    t = np.linspace(1, 0, SHOT_H)[:, None, None]    # пиксели Blender идут снизу вверх
    top, bot = np.array(SHOT_BG[0]), np.array(SHOT_BG[1])
    out = np.ones((SHOT_H, W, 4), np.float32)
    out[..., :3] = bot + (top - bot) * t
    x = 0
    for p in panels:
        al = p[..., 3:4]
        seg = out[:, x:x + p.shape[1], :3]
        seg[:] = p[..., :3] * al + seg * (1 - al)
        x += p.shape[1] + gap
    img = bpy.data.images.new('BS_preview', W, SHOT_H, alpha=False)
    try:
        img.pixels.foreach_set(out.ravel())
        img.filepath_raw = path
        img.file_format = 'PNG'
        img.save()
    finally:
        bpy.data.images.remove(img)
    return path


class BODY_OT_nexus_shot(bpy.types.Operator):
    bl_idname = 'body.nexus_shot'
    bl_label = tr('Картинка для Nexus', 'Picture for Nexus')
    bl_description = tr('Снять тело спереди, сбоку и сзади на ровном фоне и сохранить PNG рядом с модами',
                        'Shoot the body from the front, side and back on a clean background and save a PNG next to the mods')

    def execute(self, context):
        path = render_preview(shot_path())
        report(self, tr('Картинка сохранена: ', 'Picture saved: ') + path)
        if not bpy.app.background:
            try:
                subprocess.Popen(['explorer', '/select,', path])
            except Exception:
                pass
        return {'FINISHED'}


# ---------- оформление (как у Refit Studio) ----------

PAL = {'bg': '0D0D14', 'card': '16161F', 'btn': '1E1E2A', 'btn_hi': '2A2A38', 'line': '2A2A38',
       'text': 'F2F2F5', 'dim': '8E8EA3', 'acc': 'FCEE0A', 'sel': '02D7F2', 'view': '0A0A10'}
_ui = {'header': None}


def rgb(key):
    h = PAL[key]
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def set_col(obj, attr, key, alpha=1.0):
    try:
        cur = getattr(obj, attr)
        c = rgb(key)
        setattr(obj, attr, c + (alpha,) if len(cur) == 4 else c)
    except Exception:
        pass


def apply_theme():
    prefs = bpy.context.preferences
    try:
        prefs.use_preferences_save = False
    except Exception:
        pass
    th = prefs.themes[0]
    ui = th.user_interface
    for w in (ui.wcol_regular, ui.wcol_tool, ui.wcol_toggle, ui.wcol_radio, ui.wcol_option, ui.wcol_num,
              ui.wcol_numslider, ui.wcol_text, ui.wcol_menu, ui.wcol_pulldown, ui.wcol_tab, ui.wcol_list_item):
        set_col(w, 'inner', 'btn')
        set_col(w, 'inner_sel', 'acc')
        set_col(w, 'outline', 'line')
        set_col(w, 'outline_sel', 'acc')
        set_col(w, 'text', 'text')
        set_col(w, 'text_sel', 'bg')
        set_col(w, 'item', 'acc')
        try:
            w.show_shaded = False
            w.roundness = 1.0
        except Exception:
            pass
    for w in (ui.wcol_text, ui.wcol_option, ui.wcol_num, ui.wcol_numslider, ui.wcol_menu, ui.wcol_list_item):
        try:
            w.roundness = 0.45
        except Exception:
            pass
    try:
        ui.panel_roundness = 0.6
    except Exception:
        pass
    set_col(ui.wcol_text, 'inner', 'bg')
    for w in (ui.wcol_radio, ui.wcol_tab, ui.wcol_list_item, ui.wcol_menu_item, ui.wcol_option, ui.wcol_toggle,
              ui.wcol_regular):
        set_col(w, 'inner_sel', 'sel')
        set_col(w, 'outline_sel', 'sel')
        set_col(w, 'text_sel', 'bg')
    for w in (ui.wcol_text, ui.wcol_option, ui.wcol_regular, ui.wcol_toggle, ui.wcol_num, ui.wcol_numslider):
        set_col(w, 'text_sel', 'text')
    set_col(ui.wcol_numslider, 'item', 'sel')   # заполненная часть ползунка - бирюзовая
    set_col(ui.wcol_option, 'item', 'bg')
    set_col(ui.wcol_box, 'inner', 'btn')
    set_col(ui.wcol_box, 'outline', 'line')
    for w in (ui.wcol_menu_back, ui.wcol_tooltip):
        set_col(w, 'inner', 'card')
        set_col(w, 'outline', 'line')
        set_col(w, 'text', 'text')
    set_col(ui.wcol_menu_item, 'inner_sel', 'sel')
    set_col(ui.wcol_menu_item, 'text', 'text')
    set_col(ui.wcol_menu_item, 'text_sel', 'bg')
    for a, k in (('panel_back', 'card'), ('panel_sub_back', 'card'), ('panel_header', 'card'),
                 ('panel_title', 'text'), ('panel_text', 'text'), ('panel_outline', 'line'), ('panel_active', 'sel')):
        set_col(ui, a, k)
    set_col(th.regions.sidebars, 'back', 'card')
    set_col(th.regions.sidebars, 'tab_back', 'bg')
    sp = th.view_3d.space
    try:
        sp.gradients.background_type = 'SINGLE_COLOR'
    except Exception:
        pass
    set_col(sp.gradients, 'high_gradient', 'view')
    for a, k in (('header', 'bg'), ('header_text', 'text'), ('header_text_hi', 'sel'), ('text', 'text'),
                 ('text_hi', 'sel'), ('title', 'text')):
        set_col(sp, a, k)
    set_col(th.view_3d, 'object_active', 'acc')


def tidy_view(sp):
    for attr, val in (('show_region_toolbar', False), ('show_region_tool_header', False),
                      ('show_region_asset_shelf', False), ('show_region_header', True), ('show_region_ui', True)):
        try:
            setattr(sp, attr, val)
        except Exception:
            pass
    for attr in ('show_floor', 'show_axis_x', 'show_axis_y', 'show_cursor', 'show_text', 'show_stats',
                 'show_object_origins', 'show_outline_selected'):
        try:
            setattr(sp.overlay, attr, False)
        except Exception:
            pass


def header_draw(self, context):
    sc = context.scene
    lay = self.layout
    lay.label(text='BODY STUDIO', icon='OUTLINER_OB_ARMATURE')
    lay.separator()
    lay.label(text=(sc.bs_name or '?') + '  ·  ' + tr('основа: ', 'base: ') + base_label())
    if context.mode == 'SCULPT':
        lay.label(text=tr('· лепка', '· sculpting'), icon='SCULPTMODE_HLT')
    lay.separator_spacer()
    lay.operator('body.profile_save', text=tr('Сохранить профиль', 'Save profile'), icon='FILE_TICK')
    lay.operator('body.export_vortex', text=tr('В VORTEX', 'TO VORTEX'), icon='EXPORT', depress=True)


def install_keys():
    """Ctrl+S сохраняет профиль тела (как кнопка «Сохранить профиль»), а не файл сцены программы workshop.blend."""
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name='Window', space_type='EMPTY')
        if not any(k.idname == 'body.profile_save' for k in km.keymap_items):
            km.keymap_items.new('body.profile_save', 'S', 'PRESS', ctrl=True)
        if not any(k.idname == 'body.quit' for k in km.keymap_items):
            km.keymap_items.new('body.quit', 'Q', 'PRESS', ctrl=True)
    try:   # без вопроса «Сохранить изменения?» при закрытии: он перезаписал бы workshop.blend
        bpy.context.preferences.view.use_save_prompt = False
    except Exception:
        pass
    user = wm.keyconfigs.user   # настройки Blender тут не сохраняются (apply_theme), так что только на этот раз
    km = user.keymaps.get('Window') if user else None
    if km:
        for k in km.keymap_items:
            if (k.idname == 'wm.save_mainfile' and k.type == 'S' and k.ctrl) or \
                    (k.idname == 'wm.quit_blender' and k.type == 'Q' and k.ctrl):
                k.active = False


def install_header():
    if _ui['header'] is None:
        _ui['header'] = bpy.types.VIEW3D_HT_header.draw
        bpy.types.VIEW3D_HT_header.draw = header_draw


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ICON = os.path.join(ROOT, 'src', 'app.ico')
APP_EXE = os.path.join(ROOT, 'BodyStudio.exe')
APP_ID = 'BodyStudio.Workshop'
_win = {'icons': None, 'appid': set()}


def window_icon(h):
    """Значок Body Studio на окне Блендера (заголовок, панель задач, Alt+Tab). Сам Блендер не меняется."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    if _win['icons'] is None:
        u.LoadImageW.restype = wintypes.HANDLE
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SendMessageW.restype = ctypes.c_ssize_t
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        if not os.path.exists(APP_ICON):
            _win['icons'] = (None, None)
            return
        big_px = u.GetSystemMetrics(11) * 3 // 2   # чуть крупнее: панель задач на большом DPI
        _win['icons'] = (u.LoadImageW(None, APP_ICON, 1, big_px, big_px, 0x10),
                         u.LoadImageW(None, APP_ICON, 1, u.GetSystemMetrics(49), u.GetSystemMetrics(50), 0x10))
    big, small = _win['icons']
    if not big:
        return
    if u.SendMessageW(h, 0x7F, 1, 0) != big:   # WM_GETICON: Блендер мог вернуть свой
        u.SendMessageW(h, 0x80, 1, big)        # WM_SETICON, большой
        u.SendMessageW(h, 0x80, 0, small)      # маленький
    if h not in _win['appid']:
        _win['appid'].add(h)
        window_app_id(h)


def window_app_id(h):
    """Своя кнопка на панели задач, не в одной куче с обычным Блендером."""
    import ctypes, uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [('b', ctypes.c_ubyte * 16)]

    class PKEY(ctypes.Structure):
        _fields_ = [('fmtid', GUID), ('pid', wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [('vt', ctypes.c_ushort), ('r', ctypes.c_ushort * 3), ('p', ctypes.c_void_p), ('p2', ctypes.c_void_p)]

    def guid(s):
        g = GUID()
        g.b[:] = list(uuid.UUID(s).bytes_le)
        return g

    ps = ctypes.c_void_p()
    iid = guid('886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99')   # IPropertyStore
    sh = ctypes.windll.shell32
    sh.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p]
    if sh.SHGetPropertyStoreForWindow(h, ctypes.byref(iid), ctypes.byref(ps)) != 0 or not ps:
        return
    vt = ctypes.cast(ps, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PKEY),
                                   ctypes.POINTER(PROPVARIANT))(vt[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vt[7])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])
    fm = '9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3'
    keep = []
    # закрепить кнопку = запускать Body Studio, а не голый Блендер; ID ставится последним
    for pid, text in ((2, f'"{APP_EXE}"'), (3, f'{APP_EXE},0'), (4, 'Body Studio'), (5, APP_ID)):
        buf = ctypes.create_unicode_buffer(text)
        keep.append(buf)
        pv = PROPVARIANT()
        pv.vt = 31   # VT_LPWSTR
        pv.p = ctypes.addressof(buf)
        set_value(ps, ctypes.byref(PKEY(guid(fm), pid)), ctypes.byref(pv))
    commit(ps)
    release(ps)


def window_title():
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        pid, title = os.getpid(), f'Body Studio — {bpy.context.scene.bs_name or ""}'
        buf = ctypes.create_unicode_buffer(256)

        def cb(h, _):
            p = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(p))
            if p.value == pid and u.IsWindowVisible(h):
                u.GetClassNameW(h, buf, 256)
                if buf.value.startswith('GHOST'):
                    u.GetWindowTextW(h, buf, 256)
                    if buf.value != title:
                        u.SetWindowTextW(h, title)
                    try:
                        window_icon(h)
                    except Exception:
                        pass
                    try:
                        hook_close(h)
                    except Exception:
                        C.log('hook close:\n' + traceback.format_exc())
            return True
        u.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    except Exception:
        pass
    return 2.0


# ---------- вопрос при закрытии ----------
# Blender не даёт Python перехватить крестик окна, поэтому ловим само сообщение Windows о закрытии
# (WM_CLOSE: крестик, Alt+F4, «Закрыть окно» на панели задач) у окна мастерской. Если есть
# несохранённые изменения, вместо закрытия показываем свой вопрос. Ctrl+Q ведёт туда же.
WM_CLOSE = 0x0010
_close = {'hooked': {}, 'allow': False, 'last': 0.0}


def body_state():
    """Отпечаток того, что сохраняет профиль: по нему видно, менялось ли тело после сохранения."""
    sc = bpy.context.scene
    sculpt = S['sculpt'] if S.get('sculpt') is not None else np.zeros(1)
    return (tuple(round(float(getattr(sc, 'bs_' + k)), 4) for k, _, _ in BS.SLIDERS),
            hash(np.rint(sculpt * 1e5).astype(np.int64).tobytes()),   # в целых сотых мм: без «минус нуля» от погрешностей
            sc.bs_name, sc.bs_tag, bool(sc.bs_symmetry))


def mark_saved():
    """Запомнить: вот так тело выглядит сохранённым (после открытия, сохранения или загрузки профиля)."""
    try:
        S['saved_state'] = body_state()
    except Exception:
        S['saved_state'] = None


def has_changes():
    if S.get('base') is None or body_obj() is None:
        return False
    if bpy.context.mode == 'SCULPT':
        return True   # во время лепки форма попадает в тело только при выходе из лепки, считаем, что изменения есть
    sync_sculpt()
    return body_state() != S.get('saved_state')


def main_window_override():
    wm = bpy.context.window_manager
    return bpy.context.temp_override(window=bpy.context.window or wm.windows[0])


def quit_now():
    _close['allow'] = True
    with main_window_override():
        bpy.ops.wm.quit_blender()
    return None


def ask_close():
    """Окно хотят закрыть: без изменений закрываем сразу, иначе спрашиваем."""
    import time
    if time.time() - _close['last'] < 0.7:   # двойной клик по крестику - один вопрос
        return None
    _close['last'] = time.time()
    try:
        if not has_changes():
            return quit_now()
        with main_window_override():
            bpy.ops.body.quit_ask('INVOKE_DEFAULT')
    except Exception:
        C.log('close ask:\n' + traceback.format_exc())
        return quit_now()   # что-то сломалось - не держим человека в окне
    return None


def hook_close(h):
    """Подменяем обработчик сообщений окна: WM_CLOSE идёт к нам, всё остальное - как раньше."""
    if bpy.app.background or h in _close['hooked']:
        return
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL('user32')   # своя копия, чтобы типы ниже не задели другие вызовы user32
    LRESULT = ctypes.c_ssize_t
    PROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    u.CallWindowProcW.restype = LRESULT
    u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SetWindowLongPtrW.restype = ctypes.c_void_p
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    old = {}

    def proc(hwnd, msg, wp, lp):
        if msg == WM_CLOSE and not _close['allow']:
            try:
                bpy.app.timers.register(ask_close, first_interval=0.01)
                return 0
            except Exception:
                pass
        return u.CallWindowProcW(old['p'], hwnd, msg, wp, lp)

    cb = PROC(proc)
    old['p'] = u.SetWindowLongPtrW(h, -4, ctypes.cast(cb, ctypes.c_void_p))   # -4 = GWLP_WNDPROC
    _close['hooked'][h] = (cb, old)   # держим ссылку, иначе Python удалит обработчик


class BODY_OT_quit_ask(bpy.types.Operator):
    bl_idname = 'body.quit_ask'
    bl_label = tr('Закрыть мастерскую', 'Close the workshop')
    bl_description = tr('Закрыть мастерскую (спросит, сохранить ли профиль)', 'Close the workshop (asks to save a profile)')
    name: bpy.props.StringProperty(name=tr('Имя профиля', 'Profile name'), options={'SKIP_SAVE'})

    def invoke(self, context, event):
        self.name = default_profile_name()
        return context.window_manager.invoke_props_dialog(
            self, width=440, title=tr('Сохранить профиль перед выходом?', 'Save a profile before closing?'),
            confirm_text=tr('Сохранить и выйти', 'Save and close'))

    def draw(self, context):
        lay = self.layout
        lay.label(text=tr('Есть несохранённые изменения.', 'There are unsaved changes.'), icon='INFO')
        lay.label(text=tr('Без профиля в следующий раз тело начнётся с нуля.', 'Without a profile the body starts from zero next time.'))
        lay.prop(self, 'name')
        lay.separator()
        lay.operator('body.quit_now', text=tr('Выйти без сохранения', 'Close without saving'), icon='CANCEL')

    def execute(self, context):
        r = bpy.ops.body.profile_save('EXEC_DEFAULT', name=self.name)
        if r != {'FINISHED'}:
            return {'CANCELLED'}
        bpy.app.timers.register(quit_now, first_interval=0.2)
        return {'FINISHED'}


class BODY_OT_quit_now(bpy.types.Operator):
    bl_idname = 'body.quit_now'
    bl_label = tr('Выйти без сохранения', 'Close without saving')
    bl_description = tr('Закрыть мастерскую, не сохраняя изменения', 'Close the workshop without saving the changes')

    def execute(self, context):
        bpy.app.timers.register(quit_now, first_interval=0.05)
        return {'FINISHED'}


class BODY_OT_quit(bpy.types.Operator):
    bl_idname = 'body.quit'
    bl_label = tr('Закрыть мастерскую', 'Close the workshop')
    bl_description = tr('Закрыть мастерскую (Ctrl+Q)', 'Close the workshop (Ctrl+Q)')

    def execute(self, context):
        _close['last'] = 0.0
        bpy.app.timers.register(ask_close, first_interval=0.01)
        return {'FINISHED'}


# ---------- панель ----------

def big(lay, scale=1.4):
    r = lay.row(align=True)
    r.scale_y = scale
    return r


GROUPS_F = ((tr('Общее', 'General'), ('weight',)),
          (tr('Грудь', 'Breasts'), ('breast_size', 'breast_lift', 'breast_gap')),
          (tr('Туловище', 'Torso'), ('waist', 'belly', 'hips', 'butt')),
          (tr('Ноги', 'Legs'), ('thighs', 'thigh_gap', 'calves')))
# тонкие настройки (кнопка «Тонкие настройки»): дописываются в свои группы
ADVANCED_F = {tr('Грудь', 'Breasts'): ('breast_lower', 'breast_upper', 'breast_project', 'breast_width'),
              tr('Туловище', 'Torso'): ('ribcage', 'waist_height', 'flanks', 'lower_back', 'belly_lower',
                                        'butt_lift', 'butt_width', 'butt_lower'),
              tr('Ноги', 'Legs'): ('thigh_inner', 'thigh_outer', 'knees')}
# мужской V: грудные мышцы вместо груди
GROUPS_M = ((tr('Общее', 'General'), ('weight',)),
            (tr('Грудь', 'Chest'), ('breast_size',)),
            (tr('Туловище', 'Torso'), ('waist', 'belly', 'hips', 'butt')),
            (tr('Ноги', 'Legs'), ('thighs', 'thigh_gap', 'calves')))
ADVANCED_M = {tr('Грудь', 'Chest'): ('breast_project', 'breast_width', 'ribcage'),
              tr('Туловище', 'Torso'): ('waist_height', 'flanks', 'lower_back', 'belly_lower',
                                        'butt_lift', 'butt_width', 'butt_lower'),
              tr('Ноги', 'Legs'): ('thigh_inner', 'thigh_outer', 'knees')}


def groups():
    return GROUPS_M if gender() == 'male' else GROUPS_F


def advanced():
    return ADVANCED_M if gender() == 'male' else ADVANCED_F


def on_tab(self, context):
    if self.bs_tab != 'SCULPT' and context.mode == 'SCULPT':
        try:
            sync_sculpt()
        except Exception:
            pass


class BODY_PT_panel(bpy.types.Panel):
    bl_label = 'Body Studio'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = TAB
    bl_options = {'HIDE_HEADER'}

    def draw(self, context):
        sc = context.scene
        lay = self.layout
        big(lay, 1.5).prop(sc, 'bs_tab', expand=True)
        lay.separator()
        {'SLIDERS': self.draw_sliders, 'SCULPT': self.draw_sculpt, 'EXPORT': self.draw_export}[sc.bs_tab](lay, context)
        if sc.bs_status:
            lay.separator()
            box = lay.box()
            line = ''
            for w in sc.bs_status.split(' '):
                if len(line) + len(w) > 34:
                    box.label(text=line)
                    line = ''
                line += w + ' '
            if line.strip():
                box.label(text=line)

    def draw_sliders(self, lay, context):
        sc = context.scene
        row = lay.row(align=True)
        row.operator('ed.undo', text=tr('Отменить', 'Undo'), icon='LOOP_BACK')
        row.operator('ed.redo', text=tr('Вернуть', 'Redo'), icon='LOOP_FORWARDS')
        big(lay, 1.2).prop(sc, 'bs_compare', toggle=True, icon='SPLIT_HORIZONTAL',
                           text=tr('До и после: скрыть', 'Before / after: hide') if sc.bs_compare
                           else tr('До и после', 'Before / after'))
        box = lay.box()
        box.label(text=tr('Пресеты', 'Presets'), icon='PRESET')
        row = box.row(align=True)
        row.prop(sc, 'bs_preset', text='')
        row.operator('body.preset_apply', text=tr('Применить', 'Apply'))
        row = box.row(align=True)
        row.operator('body.preset_save', text=tr('Сохранить мои', 'Save mine'), icon='ADD')
        if (sc.bs_preset or '').startswith('u:'):
            row.operator('body.preset_delete', text='', icon='TRASH')
        box = lay.box()
        box.label(text=tr('Профили', 'Profiles'), icon='FILE_BACKUP')
        has = bool(chosen_profile(sc))
        row = box.row(align=True)
        row.prop(sc, 'bs_profile', text='')
        sub = row.row(align=True)
        sub.enabled = has
        sub.operator('body.profile_load', text=tr('Загрузить', 'Load'))
        row = box.row(align=True)
        row.operator('body.profile_save', text=tr('Сохранить профиль', 'Save profile'), icon='ADD')
        if has:
            row.operator('body.profile_delete', text='', icon='TRASH')
        g, adv = gender(), advanced()
        for title, keys in groups():
            box = lay.box()
            box.label(text=title)
            col = box.column(align=True)
            for k in keys:
                col.prop(sc, 'bs_' + k, text=BS.slider_label(k, g), slider=True)
            if sc.bs_advanced and title in adv:
                box.label(text=tr('тонко', 'fine'), icon='PRESET')
                col = box.column(align=True)
                for k in adv[title]:
                    col.prop(sc, 'bs_' + k, text=BS.slider_label(k, g), slider=True)
        big(lay, 1.2).prop(sc, 'bs_advanced', toggle=True, icon='PREFERENCES',
                           text=tr('Тонкие настройки: скрыть', 'Advanced: hide') if sc.bs_advanced
                           else tr('Тонкие настройки', 'Advanced'))
        big(lay, 1.2).operator('body.reset', text=tr('Сбросить ползунки', 'Reset sliders'), icon='LOOP_BACK').what = 'SLIDERS'
        hint = lay.box()
        hint.label(text=tr('Синие зоны (шея, стык с руками,', 'Blue zones (neck, arm joins,'), icon='INFO')
        hint.label(text=tr('ступни) не меняются: так голова,', 'feet) never change: so the head,'))
        hint.label(text=tr('руки и обувь подходят как раньше', 'arms and shoes fit as before'))

    def draw_sculpt(self, lay, context):
        if context.mode != 'SCULPT':
            lay.label(text=tr('Доводка формы кистями', 'Fine-tune the shape with brushes'))
            big(lay, 1.8).operator('body.sculpt', text=tr('НАЧАТЬ ЛЕПКУ', 'START SCULPTING'),
                                   icon='SCULPTMODE_HLT', depress=True)
            big(lay, 1.2).operator('body.reset', text=tr('Сбросить лепку', 'Reset sculpting'), icon='LOOP_BACK').what = 'SCULPT'
            lay.prop(context.scene, 'bs_symmetry')
            self.draw_mirror(lay)
            return
        ts = context.tool_settings.sculpt
        brush = ts.brush
        cur = current_brush()
        col = lay.column(align=True)
        for i in range(0, len(BRUSHES), 2):
            row = col.row(align=True)
            row.scale_y = 1.8
            for name, label, _ in BRUSHES[i:i + 2]:
                row.operator('body.brush', text=label, depress=(cur == name)).name = name
        if brush:
            ups = ts.unified_paint_settings
            c = lay.column(align=True)
            c.prop(ups if ups.use_unified_size else brush, 'size', text=tr('Размер кисти', 'Brush size'))
            c.prop(ups if ups.use_unified_strength else brush, 'strength', text=tr('Сила', 'Strength'), slider=True)
        ob = body_obj()
        if ob:
            lay.prop(context.scene, 'bs_symmetry')
        self.draw_mirror(lay)
        row = big(lay, 1.2)
        row.operator('ed.undo', text=tr('Отменить', 'Undo'), icon='LOOP_BACK')
        row.operator('ed.redo', text=tr('Повторить', 'Redo'), icon='LOOP_FORWARDS')
        big(lay, 1.5).operator('body.sculpt_done', text=tr('ГОТОВО', 'DONE'), icon='CHECKMARK', depress=True)
        hint = lay.box()
        hint.label(text=tr('Колёсико - приблизить', 'Wheel - zoom'), icon='MOUSE_MMB')
        hint.label(text=tr('Средняя кнопка - вращать', 'Middle button - rotate'))
        hint.label(text=tr('F - размер кисти, Shift+F - сила', 'F - brush size, Shift+F - strength'))

    def draw_mirror(self, lay):
        box = lay.box()
        box.label(text=tr('Выровнять лепку (стороны V):', 'Mirror sculpting (V sides):'), icon='MOD_MIRROR')
        row = box.row(align=True)
        row.operator('body.mirror', text=tr('Левую на правую', 'Left to right')).side = 'LEFT'
        row.operator('body.mirror', text=tr('Правую на левую', 'Right to left')).side = 'RIGHT'

    def draw_export(self, lay, context):
        sc = context.scene
        lay.prop(sc, 'bs_name', text=tr('Имя', 'Name'))
        lay.prop(sc, 'bs_tag', text=tr('Тег тела', 'Body tag'))
        lay.separator()
        big(lay, 1.8).operator('body.export_vortex', text=tr('В VORTEX', 'TO VORTEX'), icon='EXPORT', depress=True)
        big(lay, 1.3).operator('body.export_zip', text=tr('Zip для Nexus…', 'Zip for Nexus…'), icon='FILE_FOLDER')
        big(lay, 1.3).operator('body.nexus_shot', text=tr('Картинка для Nexus', 'Picture for Nexus'), icon='RENDER_STILL')
        if refit_installed():
            lay.label(text=tr('Тело само попадёт и в Refit Studio', 'The body also goes to Refit Studio'), icon='MOD_CLOTH')
        lay.separator()
        row = big(lay, 1.3)
        row.operator('body.check', text=tr('Проверить тело', 'Check the body'), icon='VIEWZOOM')
        if S.get('marked'):
            row.operator('body.unmark', text='', icon='X')
        hint = lay.box()
        if S['project'] and S['project'].get('base_mod'):
            hint.label(text=tr('Мод-основа и то, что ему нужно', 'The base body mod and what it needs'), icon='INFO')
            hint.label(text=tr('(например UV Framework), должны', '(like UV Framework) must stay'))
            hint.label(text=tr('остаться включёнными в Vortex.', 'enabled in Vortex.'))
        else:
            hint.label(text=tr('Мод заменяет обычное тело V.', 'The mod replaces the vanilla V body.'), icon='INFO')
        hint.label(text=tr('Тег пустой - как у основы.', 'Empty tag - same as the base.'))


classes = (BODY_OT_reset, BODY_OT_sculpt, BODY_OT_brush, BODY_OT_sculpt_done,
           BODY_OT_preset_apply, BODY_OT_preset_save, BODY_OT_preset_delete, BODY_OT_check, BODY_OT_unmark,
           BODY_OT_profile_save, BODY_OT_profile_load, BODY_OT_profile_delete, BODY_OT_mirror,
           BODY_OT_export_vortex, BODY_OT_export_zip, BODY_OT_nexus_shot,
           BODY_OT_quit_ask, BODY_OT_quit_now, BODY_OT_quit, BODY_PT_panel)


def logged(cls):
    inner = cls.execute

    def execute(self, context):
        try:
            res = inner(self, context)
            C.log(f'{cls.bl_idname}: {res} | {context.scene.bs_status}')
            return res
        except UserError as e:
            report(self, str(e), 'WARNING')
            return {'CANCELLED'}
        except Exception as e:
            C.log(f'{cls.bl_idname} ERROR:\n{traceback.format_exc()}')
            report(self, tr('Ошибка: ', 'Error: ') + str(e).strip().splitlines()[-1][:250] +
                   tr(' (подробности в log.txt)', ' (details in log.txt)'), 'ERROR')
            return {'CANCELLED'}
    cls.execute = execute
    return cls


@bpy.app.handlers.persistent
def after_undo(*_):
    """Ctrl+Z / «Отменить» вернули и ползунки, и форму тела. Запоминаем заново, на каких значениях
    стоит форма, иначе следующее движение ползунка «запекло» бы старое значение в лепку."""
    try:
        ob = body_obj()
        if S['base'] is None or ob is None or bpy.context.mode == 'SCULPT':
            return
        S['applied'] = values()
        S['sculpt'] = (read_co(ob) - shape(S['applied'])) * (S['base'].lock > 0)[:, None]
    except Exception:
        C.log('after undo:\n' + traceback.format_exc())


def register():
    Sc = bpy.types.Scene
    Sc.bs_status = bpy.props.StringProperty()
    Sc.bs_advanced = bpy.props.BoolProperty(
        name=tr('Тонкие настройки', 'Advanced'), default=False, options={'SKIP_SAVE'},
        description=tr('Показать больше ползунков: форма груди, бока, поясница, ягодицы, бёдра, колени',
                       'Show more sliders: breast shape, flanks, lower back, butt, thighs, knees'))
    Sc.bs_compare = bpy.props.BoolProperty(
        name=tr('До и после', 'Before / after'), default=False, options={'SKIP_SAVE'}, update=on_compare,
        description=tr('Поставить рядом исходное тело, чтобы сравнить', 'Put the original body next to yours to compare'))
    Sc.bs_preset = bpy.props.EnumProperty(name=tr('Пресет', 'Preset'), items=preset_items,
                                          description=tr('Готовый набор ползунков', 'A ready set of sliders'))
    Sc.bs_symmetry = bpy.props.BoolProperty(
        name=tr('Симметрия (левая = правая)', 'Symmetry (left = right)'), default=True, update=on_symmetry,
        description=tr('Что лепишь с одной стороны тела, повторяется с другой', 'What you sculpt on one side repeats on the other'))
    Sc.bs_profile = bpy.props.EnumProperty(name=tr('Профиль', 'Profile'), items=profile_items,
                                           description=tr('Сохранённый профиль этого тела', 'A saved profile of this body'))
    for h in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        for f in [f for f in h if getattr(f, '__name__', '') == 'after_undo']:
            h.remove(f)
        h.append(after_undo)
    Sc.bs_name = bpy.props.StringProperty(name=tr('Имя', 'Name'),
                                          description=tr('Имя твоего тела (так будет называться мод)', 'Your body name (the mod will be called this)'))
    Sc.bs_tag = bpy.props.StringProperty(
        name=tr('Тег тела', 'Body tag'),
        description=tr('Тег для Archive.XL (например MyBody). Пусто - тег мода-основы, так подходят его рефиты. '
                       'Свой тег нужен, только если делаешь рефиты именно под это тело',
                       'Archive.XL tag (e.g. MyBody). Empty - the base mod tag, so its refits keep working. '
                       'Set your own only if you make refits for this exact body'))
    Sc.bs_tab = bpy.props.EnumProperty(
        name=tr('Вкладка', 'Tab'), default='SLIDERS', update=on_tab,
        items=[('SLIDERS', tr('Ползунки', 'Sliders'), tr('Форма тела ползунками', 'Body shape with sliders')),
               ('SCULPT', tr('Лепка', 'Sculpt'), tr('Доводка кистями', 'Fine-tune with brushes')),
               ('EXPORT', tr('Экспорт', 'Export'), tr('Мод тела для игры и Refit Studio', 'Body mod for the game and Refit Studio'))])
    for k, ru, en in BS.SLIDERS:
        setattr(Sc, 'bs_' + k, bpy.props.FloatProperty(name=tr(ru, en), default=0.0, min=-1.0, max=1.0,
                                                        step=5, precision=2, update=on_slider))
    os.makedirs(C.DATA, exist_ok=True)
    for c in classes:
        if hasattr(c, 'execute'):
            logged(c)
        bpy.utils.register_class(c)


def start():
    if not bpy.app.background:
        for step in (apply_theme, install_header, install_keys):
            try:
                step()
            except Exception:
                C.log(f'{step.__name__}:\n' + traceback.format_exc())
        bpy.app.timers.register(window_title, first_interval=0.5)
    prog = None
    try:
        load_project()
        prog = Progress(tr('Открываю тело', 'Opening the body'))
        setup_scene(prog)
        bpy.context.scene.bs_status = tr('Двигай ползунки, а мелочи доводи во вкладке «Лепка». '
                                         'Готовое тело - во вкладке «Экспорт».',
                                         'Move the sliders, fine-tune details in "Sculpt". '
                                         'The finished body goes out in "Export".')
        w = deploy_warnings()
        if w:
            bpy.context.scene.bs_status = ' '.join(w)
            bpy.app.timers.register(lambda: warn_popup(w), first_interval=1.5)
    except Exception as e:
        C.log('workshop start:\n' + traceback.format_exc())
        bpy.context.scene.bs_status = tr('Ошибка запуска: ', 'Start error: ') + str(e)[:200]
    finally:
        if prog:
            prog.done()
    if arg('--test') == 'advanced':
        bpy.context.scene.bs_advanced = True
    if arg('--test') in ('sculpt', 'export'):
        bpy.app.timers.register(test_hook, first_interval=2.0)
    return None


def test_hook():
    """Только для проверки с окном (--test sculpt|export): включить режим/вкладку без мыши."""
    win = bpy.context.window_manager.windows[0]
    area = next((a for a in win.screen.areas if a.type == 'VIEW_3D'), None)
    region = next((r for r in area.regions if r.type == 'WINDOW'), None) if area else None
    try:
        with bpy.context.temp_override(window=win, area=area, region=region):
            if arg('--test') == 'sculpt':
                bpy.ops.body.sculpt('EXEC_DEFAULT')
            else:
                bpy.context.scene.bs_tab = 'EXPORT'
    except Exception:
        C.log('test hook:\n' + traceback.format_exc())
    return None


register()
if bpy.app.background:
    pass   # тесты вызывают start() сами
else:
    bpy.app.timers.register(start, first_interval=0.1)


def focus_tab(tries=[0]):
    tries[0] += 1
    done = False
    for w in bpy.context.window_manager.windows:
        for area in w.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for region in area.regions:
                if region.type == 'UI':
                    try:
                        region.active_panel_category = TAB
                        done = region.active_panel_category == TAB
                        area.tag_redraw()
                    except Exception:
                        pass
    return None if done or tries[0] > 10 else 0.5


if not bpy.app.background:
    bpy.app.timers.register(focus_tab, first_interval=0.8)
