# -*- Mode: Python; coding: utf-8; indent-tabs-mode: nil; tab-width: 4 -*-
import os
import time
import logging

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Keybinder', '3.0')

# pylint: disable=wrong-import-position, unused-argument
from gi.repository import Gtk, Gdk, GLib, Keybinder

# pylint: disable=unused-import
# these imports are needed for Gtk to find widget classes
from ulauncher.ui.ResultWidget import ResultWidget  # noqa: F401
from ulauncher.ui.SmallResultWidget import SmallResultWidget   # noqa: F401

from ulauncher.config import get_asset, get_options, FIRST_RUN
from ulauncher.ui.AppIndicator import AppIndicator
from ulauncher.ui.ItemNavigation import ItemNavigation
from ulauncher.modes.ModeHandler import ModeHandler
from ulauncher.modes.apps.AppResult import AppResult
from ulauncher.modes.extensions.ExtensionRunner import ExtensionRunner
from ulauncher.modes.extensions.ExtensionServer import ExtensionServer
from ulauncher.modes.extensions.ExtensionDownloader import ExtensionDownloader
from ulauncher.utils.Settings import Settings
from ulauncher.utils.decorator.singleton import singleton
from ulauncher.utils.timer import timer
from ulauncher.utils.wm import get_monitor, get_scaling_factor
from ulauncher.utils.icon import load_icon
from ulauncher.utils.desktop.notification import show_notification
from ulauncher.utils.environment import IS_X11_BACKEND
from ulauncher.utils.Theme import Theme, load_available_themes
from ulauncher.modes.Query import Query
from ulauncher.ui.windows.Builder import GladeObjectFactory
from ulauncher.ui.windows.WindowHelper import WindowHelper
from ulauncher.ui.windows.PreferencesWindow import PreferencesWindow

logger = logging.getLogger(__name__)


# pylint: disable=too-many-instance-attributes, too-many-public-methods, attribute-defined-outside-init
class UlauncherWindow(Gtk.ApplicationWindow, WindowHelper):
    __gtype_name__ = "UlauncherWindow"

    _current_accel_name = None
    _results_render_time = 0

    @classmethod
    @singleton
    def get_instance(cls):
        return cls()

    # Python's GTK API seems to requires non-standard workarounds like this.
    # Use finish_initializing instead of __init__.
    def __new__(cls):
        return GladeObjectFactory(cls.__name__, "ulauncher_window")

    def finish_initializing(self, ui):
        # pylint: disable=attribute-defined-outside-init
        self.ui = ui
        self.preferences = None  # instance

        self.results_nav = None
        self.window_body = self.ui['body']
        self.input = self.ui['input']
        self.prefs_btn = self.ui['prefs_btn']
        self.result_box = self.ui["result_box"]
        self.scroll_container = self.ui["result_box_scroll_container"]

        self.input.connect('changed', self.on_input_changed)
        self.prefs_btn.connect('clicked', self.on_mnu_preferences_activate)

        self.set_keep_above(True)

        self.settings = Settings.get_instance()

        self.fix_window_width()
        self.position_window()
        self.init_theme()

        # this will trigger to show frequent apps if necessary
        self.show_results([])

        self.connect('button-press-event', self.mouse_down_event)
        self.connect('button-release-event', self.mouse_up_event)
        self.connect('motion_notify_event', self.mouse_move_event)

        if self.settings.get_property('show-indicator-icon'):
            AppIndicator.get_instance(self).show()

        if IS_X11_BACKEND:
            # bind hotkey
            Keybinder.init()
            accel_name = self.settings.get_property('hotkey-show-app')
            # bind in the main thread
            GLib.idle_add(self.bind_hotkey, accel_name)

        ExtensionServer.get_instance().start()
        time.sleep(0.01)
        ExtensionRunner.get_instance().run_all()
        if not get_options().no_extensions:
            ExtensionDownloader.get_instance().download_missing()

    ######################################
    # GTK Signal Handlers
    ######################################

    # pylint: disable=unused-argument
    def on_mnu_about_activate(self, widget, data=None):
        """Display the about page for ulauncher."""
        self.activate_preferences(page='about')

    def on_mnu_preferences_activate(self, widget, data=None):
        """Display the preferences window for ulauncher."""
        self.activate_preferences(page='preferences')

    def on_preferences_destroyed(self, widget, data=None):
        '''only affects GUI

        logically there is no difference between the user closing,
        minimizing or ignoring the preferences dialog'''
        logger.debug('on_preferences_destroyed')
        # to determine whether to create or present preferences
        self.preferences = None

    def on_focus_out_event(self, widget, event):
        # apparently Gtk doesn't provide a mechanism to tell if window is in focus
        # this is a simple workaround to avoid hiding window
        # when user hits Alt+key combination or changes input source, etc.
        self.is_focused = False
        timer(0.07, lambda: self.is_focused or self.hide())

    def on_focus_in_event(self, *args):
        if self.settings.get_property('grab-mouse-pointer'):
            ptr_dev = self.get_pointer_device()
            result = ptr_dev.grab(
                self.get_window(),
                Gdk.GrabOwnership.NONE,
                True,
                Gdk.EventMask.ALL_EVENTS_MASK,
                None,
                0
            )
            logger.debug("Focus in event, grabbing pointer: %s", result)
        self.is_focused = True

    def on_input_changed(self, entry):
        """
        Triggered by user input
        """
        query = self._get_user_query()
        # This might seem odd, but this makes sure any normalization done in get_user_query() is
        # reflected in the input box. In particular, stripping out the leading white-space.
        self.input.set_text(query)
        ModeHandler.get_instance().on_query_change(query)

    # pylint: disable=inconsistent-return-statements
    def on_input_key_press_event(self, widget, event):
        keyval = event.get_keyval()
        keyname = Gdk.keyval_name(keyval[1])
        alt = event.state & Gdk.ModifierType.MOD1_MASK
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        jump_keys = self.settings.get_jump_keys()
        ModeHandler.get_instance().on_key_press_event(widget, event, self._get_user_query())

        if keyname == 'Escape':
            self.hide()

        elif ctrl and keyname == 'comma':
            self.activate_preferences()

        elif self.results_nav:
            if keyname in ('Up', 'ISO_Left_Tab') or (ctrl and keyname == 'p'):
                self.results_nav.go_up()
                return True
            if keyname in ('Down', 'Tab') or (ctrl and keyname == 'n'):
                self.results_nav.go_down()
                return True
            if alt and keyname in ('Return', 'KP_Enter'):
                self.enter_result(alt=True)
            elif keyname in ('Return', 'KP_Enter'):
                self.enter_result()
            elif alt and keyname in jump_keys:
                # on Alt+<num/letter>
                try:
                    self.select_result(jump_keys.index(keyname))
                except IndexError:
                    # selected non-existing result item
                    pass

    ######################################
    # Helpers
    ######################################

    def get_input(self):
        return self.input

    def fix_window_width(self):
        """
        Add 2px to the window width if GTK+ >= 3.20
        Because of the bug in <3.20 that doesn't add css borders to the width
        """
        width, height = self.get_size_request()
        self.set_size_request(width + 2, height)

    def init_theme(self):
        load_available_themes()
        theme = Theme.get_current()
        theme.clear_cache()

        if self.settings.get_property('disable-window-shadow'):
            self.window_body.get_style_context().add_class('no-window-shadow')

        self._render_prefs_icon()
        self.init_styles(theme.compile_css())

    def activate_preferences(self, page='preferences'):
        self.hide()

        if self.preferences is not None:
            logger.debug('Show existing preferences window')
            self.preferences.present(page=page)
        else:
            logger.debug('Create new preferences window')
            self.preferences = PreferencesWindow()
            self.preferences.set_application(self.get_application())
            self.preferences.connect('destroy', self.on_preferences_destroyed)
            self.preferences.show(page=page)
        # destroy command moved into dialog to allow for a help button

    def position_window(self):
        monitor = get_monitor(self.settings.get_property('render-on-screen') != "default-monitor")
        geo = monitor.get_geometry()
        max_height = geo.height - (geo.height * 0.15) - 100  # 100 is roughly the height of the text input
        window_width = 500 * get_scaling_factor()
        self.set_property('width-request', window_width)
        self.ui["result_box_scroll_container"].set_property('max-content-height', max_height)
        self.move(geo.width * 0.5 - window_width * 0.5 + geo.x, geo.y + geo.height * 0.12)

    def show_window(self):
        # works only when the following methods are called in that exact order
        self.present()
        self.position_window()
        if IS_X11_BACKEND:
            self.present_with_time(Keybinder.get_current_event_time())

        if not self._get_input_text():
            # make sure frequent apps are shown if necessary
            self.show_results([])
        elif self.settings.get_property('clear-previous-query'):
            self.input.set_text('')
        else:
            self.input.grab_focus()

    def mouse_down_event(self, _, event):
        """
        Prepare moving the window if the user drags
        """
        # Only on left clicks and not on the results
        if event.button == 1 and event.y < 100:
            self.set_cursor("grab")
            self.drag_start_coords = {'x': event.x, 'y': event.y}

    def bind_hotkey(self, accel_name):
        if not IS_X11_BACKEND:
            return

        if self._current_accel_name == accel_name:
            return

        if self._current_accel_name:
            Keybinder.unbind(self._current_accel_name)
            self._current_accel_name = None

        logger.info("Trying to bind app hotkey: %s", accel_name)
        Keybinder.bind(accel_name, self.show_window)
        self._current_accel_name = accel_name
        if FIRST_RUN:
            (key, mode) = Gtk.accelerator_parse(accel_name)
            display_name = Gtk.accelerator_get_label(key, mode)
            show_notification("Ulauncher", f"Hotkey is set to {display_name}")

    def _get_input_text(self):
        return self.input.get_text().lstrip()

    def _get_user_query(self):
        return Query(self._get_input_text())

    def select_result(self, index, onHover=False):
        if time.time() - self._results_render_time > 0.1:
            # Work around issue #23 -- don't automatically select item if cursor is hovering over it upon render
            self.results_nav.select(index)

    def enter_result(self, index=None, alt=False):
        if self.results_nav.enter(self._get_user_query(), index, alt=alt):
            # hide the window if it has to be closed on enter
            self.hide_and_clear_input()

    def hide(self, *args, **kwargs):
        """Override the hide method to ensure the pointer grab is released."""
        if self.settings.get_property('grab-mouse-pointer'):
            self.get_pointer_device().ungrab(0)
        super().hide(*args, **kwargs)

    def get_pointer_device(self):
        return (self
                .get_window()
                .get_display()
                .get_device_manager()
                .get_client_pointer())

    def hide_and_clear_input(self):
        self.input.set_text('')
        self.hide()

    def show_results(self, results):
        """
        :param list results: list of Result instances
        """
        self.results_nav = None
        self.result_box.foreach(lambda w: w.destroy())

        limit = len(self.settings.get_jump_keys()) or 25
        show_recent_apps = self.settings.get_property('show-recent-apps')
        recent_apps_number = int(show_recent_apps) if show_recent_apps.isnumeric() else 0
        if not self.input.get_text() and recent_apps_number > 0:
            results = AppResult.get_most_frequent(recent_apps_number)

        results = self.create_item_widgets(results, self._get_user_query())

        if results:
            self._results_render_time = time.time()
            for item in results[:limit]:
                self.result_box.add(item)
            self.results_nav = ItemNavigation(self.result_box.get_children())
            self.results_nav.select_default(self._get_user_query())

            self.result_box.set_margin_bottom(10)
            self.result_box.set_margin_top(3)
            self.apply_css(self.result_box)
            self.scroll_container.show_all()
        else:
            # Hide the scroll container when there are no results since it normally takes up a
            # minimum amount of space even if it is empty.
            self.scroll_container.hide()
        logger.debug('render %s results', len(results))

    def _render_prefs_icon(self):
        prefs_pixbuf = load_icon(get_asset('icons/gear.svg'), 16 * get_scaling_factor())
        prefs_image = Gtk.Image.new_from_pixbuf(prefs_pixbuf)
        self.prefs_btn.set_image(prefs_image)

    @staticmethod
    def create_item_widgets(items, query):
        results = []
        for index, result in enumerate(items):
            glade_filename = get_asset(f"ui/{result.UI_FILE}.ui")
            if not os.path.exists(glade_filename):
                glade_filename = None

            builder = Gtk.Builder()
            builder.set_translation_domain('ulauncher')
            builder.add_from_file(glade_filename)

            item_frame = builder.get_object('item-frame')
            item_frame.initialize(builder, result, index, query)

            results.append(item_frame)

        return results
