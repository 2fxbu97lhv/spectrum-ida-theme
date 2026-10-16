import ctypes
from ctypes import wintypes
import re
import sys

import ida_idaapi
import ida_kernwin


DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_COLOR_NONE = 0xFFFFFFFE
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_TOOLWINDOW = 0x80
WS_MINIMIZEBOX = 0x20000
WS_MAXIMIZEBOX = 0x10000
SWP_FRAME_REFRESH = 0x0237


def read_colors(stylesheet):
    match = re.search(
        r"ThemeNativeTitleBar\s*\{\s*background-color:\s*#([0-9a-fA-F]{6});"
        r"\s*color:\s*#([0-9a-fA-F]{6});\s*\}",
        stylesheet,
    )
    if match is None:
        return None
    return tuple(int.from_bytes(bytes.fromhex(rgb), "little") for rgb in match.groups())


def read_tab_icon_option(stylesheet):
    match = re.search(r"ThemeOptions\s*\{[^}]*\breplace-tab-icons:\s*(true|false)\s*;", stylesheet, re.IGNORECASE)
    return match is None or match[1].lower() == "true"


class NativeWindow:
    def __init__(self, hwnd, hide_border=True):
        self.hwnd = hwnd
        self.hide_border = hide_border
        self.original_dark_mode = None
        self.colors = None
        self.caption_styles = {}
        self.user32 = None
        dwm = ctypes.WinDLL("dwmapi")
        signature = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
        self.setter = dwm.DwmSetWindowAttribute
        self.getter = dwm.DwmGetWindowAttribute
        for function in (self.setter, self.getter):
            function.argtypes = signature
            function.restype = ctypes.c_long

    def attribute(self, function, attribute, value=0):
        data = wintypes.DWORD(value)
        result = function(self.hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
        if result < 0:
            raise OSError(f"DWM attribute {attribute} failed: 0x{result & 0xFFFFFFFF:08X}")
        return data.value

    def update(self, colors):
        if not self.hide_border:
            self.update_caption(colors is not None)
        if colors == self.colors and (colors is not None or self.original_dark_mode is None):
            return
        if colors is None:
            self.attribute(self.setter, DWMWA_BORDER_COLOR, DWMWA_COLOR_DEFAULT)
            self.attribute(self.setter, DWMWA_CAPTION_COLOR, DWMWA_COLOR_DEFAULT)
            self.attribute(self.setter, DWMWA_TEXT_COLOR, DWMWA_COLOR_DEFAULT)
            self.attribute(self.setter, DWMWA_USE_IMMERSIVE_DARK_MODE, self.original_dark_mode)
            self.original_dark_mode = None
        else:
            if self.original_dark_mode is None:
                self.original_dark_mode = self.attribute(self.getter, DWMWA_USE_IMMERSIVE_DARK_MODE)
            self.attribute(self.setter, DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
            border = DWMWA_COLOR_NONE if self.hide_border else DWMWA_COLOR_DEFAULT
            self.attribute(self.setter, DWMWA_BORDER_COLOR, border)
            self.attribute(self.setter, DWMWA_CAPTION_COLOR, colors[0])
            self.attribute(self.setter, DWMWA_TEXT_COLOR, colors[1])
        self.colors = colors

    def update_caption(self, enabled):
        if not enabled and not self.caption_styles:
            return
        if self.user32 is None:
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
            self.user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            self.user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
            self.user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            self.user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                                ctypes.c_int, ctypes.c_int, wintypes.UINT)
            self.user32.SetWindowPos.restype = wintypes.BOOL
        changed = False
        for index, mask in ((GWL_EXSTYLE, WS_EX_TOOLWINDOW), (GWL_STYLE, WS_MINIMIZEBOX | WS_MAXIMIZEBOX)):
            if not enabled and index not in self.caption_styles:
                continue
            style = self.user32.GetWindowLongPtrW(self.hwnd, index)
            original = self.caption_styles.get(index, style & mask)
            updated = style & ~mask if enabled else (style & ~mask) | original
            if updated != style:
                self.caption_styles[index] = original
                ctypes.set_last_error(0)
                previous = self.user32.SetWindowLongPtrW(self.hwnd, index, updated)
                if previous == 0 and ctypes.get_last_error():
                    raise ctypes.WinError(ctypes.get_last_error())
                changed = True
            if not enabled:
                self.caption_styles.pop(index)
        if changed and not self.user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, SWP_FRAME_REFRESH):
            raise ctypes.WinError(ctypes.get_last_error())


def set_content_padding(widget, enabled, left, right, bottom, top=0):
    property_name = "themeOriginalContentsMargins"
    original = widget.property(property_name)
    if enabled:
        if original is None:
            margins = widget.contentsMargins()
            original = [margins.left(), margins.top(), margins.right(), margins.bottom()]
            widget.setProperty(property_name, original)
        widget.setContentsMargins(original[0] + left, original[1] + top, original[2] + right, original[3] + bottom)
    elif original is not None:
        widget.setContentsMargins(*original)
        widget.setProperty(property_name, None)


def refresh_sizing():
    from PySide6 import QtCore, QtWidgets

    for widget in QtWidgets.QApplication.allWidgets():
        parent = widget.parentWidget()
        title = widget.inherits("DockWidgetTitle") or widget.inherits("DockAreaDragTitle")
        title_child = parent is not None and (parent.inherits("DockWidgetTitle") or parent.inherits("DockAreaDragTitle"))
        if not (title or title_child or isinstance(widget, (QtWidgets.QHeaderView, QtWidgets.QTabBar))):
            continue
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        if isinstance(widget, QtWidgets.QHeaderView):
            QtCore.QCoreApplication.sendEvent(widget.viewport(), QtCore.QEvent(QtCore.QEvent.Type.StyleChange))
        widget.updateGeometry()


def update_tab_close_buttons(bar, enabled):
    from PySide6 import QtWidgets

    side = QtWidgets.QTabBar.ButtonPosition.RightSide
    for index in range(bar.count()):
        current = bar.tabButton(index, side)
        if current is None:
            continue
        original = current.property("themeOriginalTabClose")
        if not enabled:
            if original is not None:
                original.setParent(bar)
                bar.setTabButton(index, side, original)
                current.deleteLater()
            continue
        if original is None:
            holder = QtWidgets.QWidget(bar)
            holder.setObjectName("ThemeTabCloseControl")
            holder.setProperty("themeOriginalTabClose", current)
            layout = QtWidgets.QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 4, 0)
            layout.setSpacing(0)
            button = QtWidgets.QPushButton(holder)
            button.setObjectName("ThemeTabCloseButton")
            button.setToolTip(current.toolTip())
            layout.addWidget(button)

            def close_tab(checked=False, container=holder, tabs=bar):
                for tab in range(tabs.count()):
                    if tabs.tabButton(tab, side) is container:
                        tabs.tabCloseRequested.emit(tab)
                        return

            button.clicked.connect(close_tab)
            current.setParent(holder)
            bar.setTabButton(index, side, holder)
            current = holder
        current.ensurePolished()
        current.setFixedSize(current.sizeHint())


def create_tab_icon(bar, icon):
    from PySide6 import QtCore, QtGui, QtWidgets

    class TabIcon(QtWidgets.QWidget):
        def paintEvent(self, event):
            selected = bar.tabButton(bar.currentIndex(), QtWidgets.QTabBar.ButtonPosition.LeftSide) is self
            index = bar.tabAt(self.geometry().center())
            mode = QtGui.QIcon.Mode.Normal if bar.isTabEnabled(index) else QtGui.QIcon.Mode.Disabled
            state = QtGui.QIcon.State.On if selected else QtGui.QIcon.State.Off
            painter = QtGui.QPainter(self)
            self.property("themeTabIcon").paint(painter, self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, mode, state)

    widget = TabIcon(bar)
    widget.setFixedSize(bar.iconSize())
    widget.setProperty("themeTabIcon", icon)
    widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    bar.currentChanged.connect(widget.update)
    return widget


def update_tab_icons(bar, enabled):
    from PySide6 import QtGui, QtWidgets

    side = QtWidgets.QTabBar.ButtonPosition.LeftSide
    for index in range(bar.count()):
        widget = bar.tabButton(index, side)
        original = widget.property("themeTabIcon") if widget is not None else None
        if not enabled:
            if original is not None:
                if bar.tabIcon(index).isNull():
                    bar.setTabIcon(index, original)
                bar.setTabButton(index, side, None)
                widget.deleteLater()
            continue
        icon = bar.tabIcon(index)
        if not icon.isNull() and (widget is None or original is not None):
            if widget is None:
                widget = create_tab_icon(bar, icon)
                bar.setTabButton(index, side, widget)
            else:
                widget.setProperty("themeTabIcon", icon)
            bar.setTabIcon(index, QtGui.QIcon())
            widget.update()
        if widget is not None and widget.property("themeTabIcon") is not None:
            widget.setFixedSize(bar.iconSize())


def iter_ida_widgets(widget_type):
    from PySide6 import QtWidgets

    for widget in QtWidgets.QApplication.allWidgets():
        view = ida_kernwin.PluginForm.QtWidgetToTWidget(widget)
        if ida_kernwin.get_widget_type(view) == widget_type:
            yield widget, view


def update_title_buttons(enabled, window):
    from PySide6 import QtCore, QtWidgets

    output_docks = set()
    if enabled:
        for widget, _ in iter_ida_widgets(ida_kernwin.BWN_OUTPUT):
            parent = widget.parentWidget()
            while parent is not None:
                if parent.inherits("IDADockWidget"):
                    output_docks.add(parent)
                    break
                parent = parent.parentWidget()
    bars = []
    for title in QtWidgets.QApplication.allWidgets():
        if title.inherits("DockTabBar"):
            bars.append(title)
            continue
        tab_group = title.inherits("DockTab")
        if not (tab_group or title.inherits("DockWidgetTitle") or title.inherits("DockAreaDragTitle")):
            continue
        detached = title.window() is not window
        state = detached if enabled else None
        output = title.inherits("DockWidgetTitle") and title.parentWidget() in output_docks
        if title.property("themeDetached") != state or title.property("themeOutputTitle") != output:
            title.setProperty("themeDetached", state)
            title.setProperty("themeOutputTitle", output)
            title.style().unpolish(title)
            title.style().polish(title)
            QtCore.QCoreApplication.sendEvent(title, QtCore.QEvent(QtCore.QEvent.Type.StyleChange))
        if tab_group:
            layout = title.layout()
            original = layout.property("themeOriginalTabLayout")
            if enabled:
                if original is None:
                    margins = layout.contentsMargins()
                    original = [margins.left(), margins.top(), margins.right(), margins.bottom(), layout.spacing()]
                    layout.setProperty("themeOriginalTabLayout", original)
                layout.setContentsMargins(original[0], original[1], original[2], original[3] + 2)
                layout.setSpacing(0 if detached else 2)
            elif original is not None:
                layout.setContentsMargins(*original[:4])
                layout.setSpacing(original[4])
                layout.setProperty("themeOriginalTabLayout", None)
            continue
        if title.inherits("DockAreaDragTitle"):
            original = title.property("themeOriginalDragTitleVisible")
            if enabled and detached:
                if original is None:
                    title.setProperty("themeOriginalDragTitleVisible", not title.isHidden())
                title.hide()
            elif original is not None:
                if detached:
                    title.setVisible(original)
                title.setProperty("themeOriginalDragTitleVisible", None)
        for label in title.findChildren(QtWidgets.QLabel, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly):
            if label.text() != "Drag this title to dock somewhere else":
                continue
            original = label.property("themeDockHintVisible")
            if enabled:
                if original is None:
                    label.setProperty("themeDockHintVisible", not label.isHidden())
                label.hide()
            elif original is not None:
                label.setVisible(original)
                label.setProperty("themeDockHintVisible", None)
        layout = title.layout()
        if layout is not None:
            original = layout.property("themeOriginalOutputTitleMargins")
            if output:
                if original is None:
                    margins = layout.contentsMargins()
                    original = [margins.left(), margins.top(), margins.right(), margins.bottom()]
                    layout.setProperty("themeOriginalOutputTitleMargins", original)
                layout.setContentsMargins(original[0], 1, original[2], 1)
            elif original is not None:
                layout.setContentsMargins(*original)
                layout.setProperty("themeOriginalOutputTitleMargins", None)
            layout.invalidate()
            layout.activate()
        if enabled and title.isVisible():
            bounds = title.contentsRect()
            children = title.findChildren(QtWidgets.QWidget,
                                          options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly)
            for child in children:
                if isinstance(child, (QtWidgets.QAbstractButton, QtWidgets.QLabel)) and child.isVisible():
                    child.move(child.x(), bounds.top() + (bounds.height() - child.height()) // 2)
    replace_icons = enabled and read_tab_icon_option(QtWidgets.QApplication.instance().styleSheet())
    for bar in bars:
        update_tab_close_buttons(bar, replace_icons)
        update_tab_icons(bar, replace_icons)
        if replace_icons:
            bar.setProperty("themeReplacedTabIcons", True)
            for index in range(bar.count()):
                icon = bar.tabButton(index, QtWidgets.QTabBar.ButtonPosition.LeftSide)
                if icon is not None:
                    bounds = bar.tabRect(index)
                    x = icon.x()
                    if icon.property("themeTabIcon") is not None:
                        option = QtWidgets.QStyleOptionTab()
                        bar.initStyleOption(option, index)
                        text_rect = bar.style().subElementRect(QtWidgets.QStyle.SubElement.SE_TabBarTabText, option, bar)
                        x = text_rect.left() - icon.width() - 4 if bar.isLeftToRight() else text_rect.right() + 5
                    icon.move(x, bounds.top() + (bounds.height() - icon.height()) // 2)
        elif bar.property("themeReplacedTabIcons"):
            bar.setProperty("themeReplacedTabIcons", None)
            QtCore.QCoreApplication.sendEvent(bar, QtCore.QEvent(QtCore.QEvent.Type.StyleChange))


def update_status_font(status, window, enabled):
    from PySide6 import QtWidgets

    parent = status.parentWidget()
    pane = parent is not window
    changed = False
    labels = status.findChildren(QtWidgets.QLabel)
    for label in ([status] if pane else []) + labels:
        original = label.property("themeOriginalStatusFont")
        if enabled:
            if original is None:
                label.setProperty("themeOriginalStatusFont", label.font())
            font = QtWidgets.QApplication.font()
        elif original is not None:
            font = original
            label.setProperty("themeOriginalStatusFont", None)
        else:
            continue
        if label.font() != font:
            label.setFont(font)
            changed = True
    if pane:
        old_height = status.minimumHeight()
        original = status.property("themeOriginalStatusMinimumHeight")
        if enabled:
            if original is None:
                original = status.minimumHeight()
                status.setProperty("themeOriginalStatusMinimumHeight", original)
            status.setMinimumHeight(max(original, 23))
        elif original is not None:
            status.setMinimumHeight(original)
            status.setProperty("themeOriginalStatusMinimumHeight", None)
        changed |= status.minimumHeight() != old_height
        if changed:
            status.updateGeometry()


def color_data_tokens(original, cfunc, index, enabled):
    import ida_hexrays
    import ida_lines
    colors = ida_lines.SCOLOR_DEMNAME + ida_lines.SCOLOR_DNAME
    pattern = re.compile('\x01([' + re.escape(colors) + '])([^\x01\x02]*)\x02\\1')

    def replace(match):
        item = ida_hexrays.ctree_item_t()
        column = len(ida_lines.tag_remove(original[:match.start()]))
        if (cfunc.get_line_item(original, column, index >= cfunc.hdrlines, None, item, None)
                and item.citype == ida_hexrays.VDI_EXPR
                and item.e.op == ida_hexrays.cot_obj):
            function = item.e.type.is_func() or item.e.type.is_funcptr()
            target = ida_lines.SCOLOR_DNAME if enabled and not function else ida_lines.SCOLOR_DEMNAME
            return '\x01' + target + match[2] + '\x02' + target
        return match[0]

    return pattern.sub(replace, original)


def color_type_tokens(line, is_type, enabled, stack):
    import ida_lines

    type_roles = (ida_lines.SCOLOR_HIDNAME, ida_lines.SCOLOR_REG)
    type_color = ida_lines.SCOLOR_MACRO
    type_colors = dict(zip(type_roles, (type_color, ida_lines.SCOLOR_ASMDIR)))
    original_roles = {color: role for role, color in type_colors.items()}
    tokens = re.finditer(r'\x01\([0-9a-fA-F]{' + str(ida_lines.COLOR_ADDR_SIZE)
                         + r'}|\x03.|\x01.|\x02.|[^\x01-\x03]+', line, re.S)
    output = []
    for token in tokens:
        text = token[0]
        if text.startswith('\x01(') or text.startswith('\x03'):
            output.append(text)
        elif text.startswith('\x01'):
            inserted = text[1] == type_color and bool(stack) and stack[-1] in type_roles
            role = text[1] if inserted else original_roles.get(text[1], text[1])
            stack.append(role)
            if not inserted:
                output.append('\x01' + role)
        elif text.startswith('\x02'):
            role = text[1]
            if role not in stack:
                role = original_roles.get(role, role)
            if role not in stack:
                output.append(text)
                continue
            index = len(stack) - 1 - stack[::-1].index(role)
            inserted = role == type_color and index > 0 and stack[index - 1] in type_roles
            del stack[index:]
            if not inserted:
                output.append('\x02' + role)
        else:
            in_type = stack and (stack[-1] in type_roles
                                  or (stack[-1] == type_color and len(stack) > 1 and stack[-2] in type_roles))
            if enabled and in_type:
                role = stack[-1] if stack[-1] in type_roles else stack[-2]
                color = type_colors[role]
                text = re.sub(r'[A-Za-z_$][\w$]*(?:::[A-Za-z_$][\w$]*)*',
                              lambda m: '\x02' + role + '\x01' + color + m[0]
                              + '\x02' + color + '\x01' + role
                              if is_type(m[0]) else m[0], text)
            output.append(text)
    roles = '[' + re.escape(''.join(type_roles)) + ']'
    result = re.sub(r'\x01(' + roles + r')\x02\1', '', ''.join(output))
    return re.sub(r'\x02(' + roles + r')\x01\1', '', result)


def type_predicate(cfunc):
    import ida_typeinf

    variables = {variable.name for variable in cfunc.get_lvars()}
    known = dict.fromkeys(('void bool char wchar_t char16_t char32_t short int long float double '
                          'signed unsigned __int8 __int16 __int32 __int64 __int128 '
                          '_BYTE _WORD _DWORD _QWORD _OWORD _YMMWORD _ZMMWORD _TBYTE '
                          '_BOOL1 _BOOL2 _BOOL4 _BOOL8 _UNKNOWN').split(), True)

    def is_type(name):
        if name in variables:
            return False
        if name not in known:
            known[name] = ida_typeinf.tinfo_t().get_named_type(None, name)
        return known[name]

    return is_type


def color_number_tokens(line, enabled):
    import ida_lines

    keyword_on = '\x01' + ida_lines.SCOLOR_KEYWORD
    keyword_off = '\x02' + ida_lines.SCOLOR_KEYWORD
    for role in (ida_lines.SCOLOR_NUMBER, ida_lines.SCOLOR_DNUM):
        number_on = '\x01' + role
        number_off = '\x02' + role
        line = re.sub(re.escape(keyword_on + number_on) + r'([^\x01\x02]*)'
                      + re.escape(number_off + keyword_off),
                      lambda m: keyword_on + m[1] + keyword_off, line)
        line = re.sub(re.escape(number_on) + r'([^\x01\x02]*)' + re.escape(number_off),
                      lambda m: keyword_on + m[1] + keyword_off, line)
    if not enabled:
        return line
    number_on = '\x01' + ida_lines.SCOLOR_DNUM
    number_off = '\x02' + ida_lines.SCOLOR_DNUM
    literal = re.compile(r'[+-]?(?:0x[0-9a-f]+|0b[01]+|(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)'
                         r'(?:u(?:ll?|i64)?|ll?u?|i64|[fd])?', re.I)
    return re.sub(re.escape(keyword_on) + r'([^\x01\x02]*)' + re.escape(keyword_off),
                  lambda m: number_on + m[1] + number_off
                  if literal.fullmatch(m[1]) else m[0], line)


def color_pseudocode(cfunc, enabled):
    is_type = type_predicate(cfunc) if enabled else None
    type_stack = []
    changed = False
    for index, line in enumerate(cfunc.get_pseudocode()):
        original = line.line
        updated = color_data_tokens(original, cfunc, index, enabled)
        updated = color_type_tokens(updated, is_type, enabled, type_stack)
        updated = color_number_tokens(updated, enabled)
        if updated != original:
            line.line = updated
            changed = True
    return changed


def create_pseudocode_hook(app):
    import ida_hexrays

    if not ida_hexrays.init_hexrays_plugin():
        return None

    class PseudocodeColors(ida_hexrays.Hexrays_Hooks):
        def func_printed(self, cfunc):
            if read_colors(app.styleSheet()) is not None:
                color_pseudocode(cfunc, True)
            return 0

        def open_pseudocode(self, vu):
            color_pseudocode(vu.cfunc, read_colors(app.styleSheet()) is not None)
            return 0

        def switch_pseudocode(self, vu):
            return self.open_pseudocode(vu)

        def refresh_pseudocode(self, vu):
            if color_pseudocode(vu.cfunc, read_colors(app.styleSheet()) is not None):
                ida_kernwin.refresh_custom_viewer(vu.ct)
            return 0

        def update_view(self, view, enabled):
            vu = ida_hexrays.get_widget_vdui(view)
            if vu is not None and vu.cfunc:
                if color_pseudocode(vu.cfunc, enabled):
                    ida_kernwin.refresh_custom_viewer(view)

        def update_open_views(self, enabled):
            for _, view in iter_ida_widgets(ida_kernwin.BWN_PSEUDOCODE):
                self.update_view(view, enabled)

        def stop(self):
            self.unhook()
            self.update_open_views(False)

    hook = PseudocodeColors()
    hook.hook()
    hook.update_open_views(read_colors(app.styleSheet()) is not None)
    return hook


def create_watcher(window, restart):
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance()

    class ThemeWatcher(QtCore.QObject):
        def __init__(self):
            super().__init__(window)
            self.natives = {}
            self.windows_supported = (sys.platform == "win32"
                                      and sys.getwindowsversion().platform_version[2] >= 22000)
            self.popout_padding = set()
            self.restart_requested = False
            self.sizing_requested = True
            self.title_alignment_requested = True
            self.output_requested = True
            self.updating_sizing = False
            self.refreshing = False
            self.pseudocode_hook = create_pseudocode_hook(app)
            self.timer = QtCore.QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(self.refresh)
            app.installEventFilter(self)
            self.schedule()

        def schedule(self):
            self.timer.start(0)

        def ensure_pseudocode(self):
            if self.pseudocode_hook is None:
                self.pseudocode_hook = create_pseudocode_hook(app)
            return self.pseudocode_hook

        def eventFilter(self, watched, event):
            if (isinstance(watched, QtWidgets.QStatusBar) and event.type() == QtCore.QEvent.Type.Polish
                    and read_colors(app.styleSheet()) is not None):
                update_status_font(watched, window, True)
            if (isinstance(watched, QtWidgets.QWidget) and watched.inherits("DockSplitter")
                    and event.type() in (QtCore.QEvent.Type.Show, QtCore.QEvent.Type.ParentChange)):
                self.schedule()
            if (isinstance(watched, QtWidgets.QStatusBar)
                    and event.type() in (QtCore.QEvent.Type.Show, QtCore.QEvent.Type.LayoutRequest)):
                self.schedule()
            if (isinstance(watched, QtWidgets.QWidget) and watched.inherits("ConsoleWidget")
                    and event.type() in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.LayoutRequest,
                                         QtCore.QEvent.Type.Show, QtCore.QEvent.Type.ParentChange)):
                self.output_requested = True
                self.title_alignment_requested = True
                self.schedule()
            if (isinstance(watched, QtWidgets.QWidget)
                    and (watched.inherits("DockWidgetTitle") or watched.inherits("DockAreaDragTitle")
                         or watched.inherits("DockTab") or watched.inherits("DockTabBar")
                         or watched.inherits("IDADockWidget") or watched.inherits("DockSplitter"))
                    and event.type() in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.LayoutRequest,
                                         QtCore.QEvent.Type.Show, QtCore.QEvent.Type.ParentChange)):
                self.title_alignment_requested = True
                self.schedule()
            if not self.updating_sizing and event.type() in (
                    QtCore.QEvent.Type.FontChange,
                    QtCore.QEvent.Type.ApplicationFontChange,
                    QtCore.QEvent.Type.DevicePixelRatioChange,
                    QtCore.QEvent.Type.ScreenChangeInternal):
                self.sizing_requested = True
                self.schedule()
            if watched is window and event.type() == QtCore.QEvent.Type.StyleChange:
                self.restart_requested = True
            if (isinstance(watched, QtWidgets.QDockWidget)
                    and watched.objectName() == "Navigator"
                    and event.type() in (QtCore.QEvent.Type.Resize,
                                         QtCore.QEvent.Type.Show,
                                         QtCore.QEvent.Type.LayoutRequest)):
                self.schedule()
            if (isinstance(watched, QtWidgets.QWidget)
                    and (watched.isWindow() or watched in self.natives)
                    and event.type() in (
                        QtCore.QEvent.Type.StyleChange,
                        QtCore.QEvent.Type.PaletteChange,
                        QtCore.QEvent.Type.Show,
                        QtCore.QEvent.Type.WinIdChange,
                        QtCore.QEvent.Type.Resize,
                    )):
                if event.type() in (QtCore.QEvent.Type.Show, QtCore.QEvent.Type.WinIdChange):
                    self.output_requested = True
                self.schedule()
            if (event.type() == QtCore.QEvent.Type.Show and self.timer.isActive()
                    and not self.refreshing and isinstance(watched, QtWidgets.QWidget)
                    and (watched.isWindow() or isinstance(watched, QtWidgets.QStatusBar)
                         or watched.inherits("ConsoleWidget") or watched.inherits("DockTab")
                         or watched.inherits("DockTabBar") or watched.inherits("DockWidgetTitle")
                         or watched.inherits("DockSplitter")
                         or watched.inherits("DockAreaDragTitle"))):
                self.timer.stop()
                self.refresh()
            return False

        def update_windows(self, colors):
            native_colors = colors if self.windows_supported else None
            targets = {}
            padding_targets = set()
            for widget in app.topLevelWidgets():
                flags = widget.windowFlags()
                kind = flags & QtCore.Qt.WindowType.WindowType_Mask
                if kind in (QtCore.Qt.WindowType.Popup, QtCore.Qt.WindowType.ToolTip):
                    continue
                hwnd = int(widget.internalWinId())
                if (hwnd and flags & QtCore.Qt.WindowType.WindowTitleHint
                        and not flags & QtCore.Qt.WindowType.FramelessWindowHint):
                    targets[widget] = hwnd
            removed = self.natives.keys() - targets.keys()
            existing_widgets = set(app.allWidgets()) if removed else ()
            for widget in removed:
                if widget is not window and widget in existing_widgets:
                    self.update_caption_divider(widget, False)
            self.natives = {widget: native for widget, native in self.natives.items()
                            if targets.get(widget) == native.hwnd}
            for widget, hwnd in targets.items():
                if widget is not window:
                    if colors is not None:
                        if widget.inherits("DockArea"):
                            padding_targets.update(child for child in widget.findChildren(
                                QtWidgets.QWidget, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly)
                                if child.inherits("DockSplitter"))
                        else:
                            padding_targets.add(widget)
                self.update_caption_divider(widget, colors is not None)
                if widget not in self.natives:
                    if native_colors is None:
                        continue
                    self.natives[widget] = NativeWindow(hwnd, hide_border=widget is window)
                try:
                    self.natives[widget].update(native_colors)
                except OSError:
                    pass
            unpadded = self.popout_padding - padding_targets
            if unpadded:
                live_widgets = set(app.allWidgets())
                for widget in unpadded & live_widgets:
                    set_content_padding(widget, False, 3, 3, 3, top=1)
            for widget in padding_targets:
                set_content_padding(widget, True, 3, 3, 3, top=1)
            self.popout_padding = padding_targets
            if native_colors is None:
                self.natives.clear()

        def update_caption_divider(self, widget, enabled):
            divider = widget.findChild(QtWidgets.QFrame, "ThemeCaptionDivider",
                                       QtCore.Qt.FindChildOption.FindDirectChildrenOnly)
            if enabled:
                if divider is None:
                    divider = QtWidgets.QFrame(widget)
                    divider.setObjectName("ThemeCaptionDivider")
                    divider.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                divider.setGeometry(0, 0, widget.width(), 1)
                divider.show()
                divider.raise_()
            elif divider is not None:
                divider.hide()

        def refresh(self):
            if self.restart_requested:
                self.restart_requested = False
                restart()
                return
            colors = read_colors(app.styleSheet())
            self.refreshing = True
            try:
                if self.sizing_requested and colors is not None:
                    self.sizing_requested = False
                    self.updating_sizing = True
                    try:
                        refresh_sizing()
                    finally:
                        self.updating_sizing = False
                    self.title_alignment_requested = True
                if self.title_alignment_requested and colors is not None:
                    self.title_alignment_requested = False
                    update_title_buttons(True, window)
                self.update_navigator(colors is not None)
                self.update_status_padding(colors is not None)
                if self.output_requested:
                    self.output_requested = False
                    self.update_output(colors is not None)
                self.update_windows(colors)
            finally:
                self.refreshing = False

        def update_output(self, enabled):
            for console, _ in iter_ida_widgets(ida_kernwin.BWN_OUTPUT):
                if not console.inherits("ConsoleWidget"):
                    continue
                detached = console.window() is not window
                set_content_padding(console, enabled, 0 if detached else 1, 0 if detached else 1,
                                    0 if detached else 5, top=0 if detached else 3)
                for child in console.findChildren(QtWidgets.QWidget):
                    if child.inherits("CLIWidget"):
                        set_content_padding(child, enabled, 0, 1, 0)
                outline = console.findChild(QtWidgets.QFrame, "ThemeOutputOutline",
                                            QtCore.Qt.FindChildOption.FindDirectChildrenOnly)
                if enabled and not detached:
                    if outline is None:
                        outline = QtWidgets.QFrame(console)
                        outline.setObjectName("ThemeOutputOutline")
                        outline.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                    outline.setGeometry(console.rect().adjusted(0, 0, 0, -2))
                    outline.show()
                    outline.raise_()
                elif outline is not None:
                    outline.hide()

        def update_navigator(self, enabled):
            dock = window.findChild(QtWidgets.QDockWidget, "Navigator")
            if dock is None:
                return
            content = dock.widget()
            if content is not None:
                set_content_padding(content, enabled, 0, 0, 2)
            divider = dock.findChild(QtWidgets.QFrame, "ThemeNavigatorDivider")
            if enabled:
                if divider is None:
                    divider = QtWidgets.QFrame(dock)
                    divider.setObjectName("ThemeNavigatorDivider")
                    divider.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                divider.setGeometry(0, dock.height() - 1, dock.width(), 1)
                divider.show()
                divider.raise_()
            elif divider is not None:
                divider.hide()
            return divider

        def update_status_padding(self, enabled):
            status = window.findChild(QtWidgets.QStatusBar, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly)
            if status is not None:
                set_content_padding(status, enabled, 4, 4, 4)
            for status in window.findChildren(QtWidgets.QStatusBar):
                update_status_font(status, window, enabled)

        def stop(self):
            self.timer.stop()
            app.removeEventFilter(self)
            if self.pseudocode_hook is not None:
                self.pseudocode_hook.stop()
            divider = self.update_navigator(False)
            if divider is not None:
                divider.setObjectName("")
                divider.deleteLater()
            self.update_status_padding(False)
            self.update_output(False)
            update_title_buttons(False, window)
            self.update_windows(None)
            self.deleteLater()

    return ThemeWatcher()


class ThemeHooks(ida_kernwin.UI_Hooks):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def ready_to_run(self):
        self.plugin.start()
        if self.plugin.watcher is not None:
            self.plugin.watcher.ensure_pseudocode()

    def current_widget_changed(self, widget, prev_widget):
        watcher = self.plugin.watcher
        if (watcher is not None and widget is not None
                and ida_kernwin.get_widget_type(widget) == ida_kernwin.BWN_PSEUDOCODE):
            from PySide6 import QtWidgets
            hook = watcher.ensure_pseudocode()
            if hook is not None:
                enabled = read_colors(QtWidgets.QApplication.instance().styleSheet()) is not None
                hook.update_view(widget, enabled)


class ThemeHelper(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_FIX | ida_idaapi.PLUGIN_HIDE
    comment = ""
    help = ""
    wanted_name = "Theme helper"
    wanted_hotkey = ""

    def init(self):
        self.hooks = None
        self.watcher = None
        if not ida_kernwin.is_idaq():
            return ida_idaapi.PLUGIN_SKIP
        self.hooks = ThemeHooks(self)
        self.hooks.hook()
        self.start()
        return ida_idaapi.PLUGIN_KEEP

    def start(self):
        from PySide6 import QtWidgets

        if self.watcher is None:
            window = QtWidgets.QWidget.find(ida_kernwin.get_window_id())
            if window is not None:
                self.window = window
                self.watcher = create_watcher(window, self.restart)

    def restart(self):
        self.watcher.stop()
        self.watcher = create_watcher(self.window, self.restart)

    def run(self, arg):
        self.start()
        if self.watcher is not None:
            self.restart()

    def term(self):
        if self.hooks is not None:
            self.hooks.unhook()
        if self.watcher is not None:
            self.watcher.stop()


def PLUGIN_ENTRY():
    return ThemeHelper()
