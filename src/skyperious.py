# -*- coding: utf-8 -*-
"""
Skyperious application main window class and all project-specific UI classes.

------------------------------------------------------------------------------
This file is part of Skyperious - a Skype database viewer and merger.
Released under the MIT License.

@author      Erki Suurjaak
@created     26.11.2011
@modified    15.09.2013
------------------------------------------------------------------------------
"""
import base64
import collections
import copy
import datetime
import hashlib
import math
import os
import re
import shutil
import sys
import textwrap
import time
import traceback
import urllib
import webbrowser
import wx
import wx.gizmos
import wx.grid
import wx.html
import wx.lib
import wx.lib.agw.fmresources
import wx.lib.agw.genericmessagedialog
import wx.lib.agw.labelbook
import wx.lib.agw.flatmenu
import wx.lib.agw.flatnotebook
import wx.lib.agw.ultimatelistctrl
import wx.lib.newevent
import wx.lib.scrolledpanel
import wx.stc

# Core functionality can work without these modules
try:
    import Skype4Py
except ImportError:
    Skype4Py = None
try:
    from dateutil.relativedelta import relativedelta
except ImportError:
    relativedelta = None

from third_party import step

import conf
import controls
import export
import guibase
import images
import main
import skypedata
import support
import templates
import util
import workers


"""Custom application events for worker results."""
WorkerEvent, EVT_WORKER = wx.lib.newevent.NewEvent()
ContactWorkerEvent, EVT_CONTACT_WORKER = wx.lib.newevent.NewEvent()
DetectionWorkerEvent, EVT_DETECTION_WORKER = wx.lib.newevent.NewEvent()
OpenDatabaseEvent, EVT_OPEN_DATABASE = wx.lib.newevent.NewEvent()


class MainWindow(guibase.TemplateFrameMixIn, wx.Frame):
    """Skyperious main window."""

    def __init__(self):
        wx.Frame.__init__(self, parent=None, title=conf.Title, size=conf.WindowSize)
        guibase.TemplateFrameMixIn.__init__(self)

        self.db_filename = None # Current selected file in main list
        self.db_filenames = {}  # added DBs {filename: {size, last_modified,
                                #            account, chats, messages, error},}
        self.dbs = {}           # Open databases {filename: SkypeDatabase, }
        self.db_pages = {}      # {DatabasePage: SkypeDatabase, }
        self.merger_pages = {}  # {MergerPage: (SkypeDatabase, SkypeDatabase),}
        self.page_merge_latest = None # Last opened merger page
        self.page_db_latest = None    # Last opened database page
        # List of Notebook pages user has visited, used for choosing page to
        # show when closing one.
        self.pages_visited = []

        icons = images.get_appicons()
        self.SetIcons(icons)

        panel = self.panel_main = wx.Panel(self)
        sizer = panel.Sizer = wx.BoxSizer(wx.VERTICAL)

        self.frame_console.SetIcons(icons)

        notebook = self.notebook = wx.lib.agw.flatnotebook.FlatNotebook(
            parent=panel, style=wx.NB_TOP,
            agwStyle=wx.lib.agw.flatnotebook.FNB_NODRAG |
                     wx.lib.agw.flatnotebook.FNB_NO_X_BUTTON |
                     wx.lib.agw.flatnotebook.FNB_MOUSE_MIDDLE_CLOSES_TABS |
                     wx.lib.agw.flatnotebook.FNB_NO_TAB_FOCUS |
                     wx.lib.agw.flatnotebook.FNB_FF2
        )

        self.create_page_main(notebook)
        self.create_page_log(notebook)
        notebook.RemovePage(self.notebook.GetPageCount() - 1) # Hide log window initially
        # Kludge for being able to close log window repeatedly, as DatabasePage
        # or MergerPage get deleted on closing automatically.
        self.page_log.is_hidden = True

        sizer.Add(notebook, proportion=1, flag=wx.GROW | wx.RIGHT | wx.BOTTOM)
        self.create_menu()

        self.dialog_selectfolder = wx.DirDialog(
            parent=self,
            message="Choose a directory where to search for Skype databases",
            defaultPath=os.getcwd(),
            style=wx.DD_DIR_MUST_EXIST | wx.RESIZE_BORDER)
        self.dialog_savefile = wx.FileDialog(
            parent=self, defaultDir=os.getcwd(), defaultFile="",
            style=wx.FD_OVERWRITE_PROMPT | wx.FD_SAVE | wx.RESIZE_BORDER)

        self.skype_handler = SkypeHandler() if Skype4Py else None
        # Memory file system for showing images in wx.HtmlWindow
        self.memoryfs = {"files": {}, "handler": wx.MemoryFSHandler()}
        wx.FileSystem_AddHandler(self.memoryfs["handler"])
        abouticon = "skyperious.png" # Program icon shown in About window
        raw = base64.b64decode(images.Icon48x48_32bit.data)
        self.memoryfs["handler"].AddFile(abouticon, raw, wx.BITMAP_TYPE_PNG)
        self.memoryfs["files"][abouticon] = 1
        # Images shown on the default search content page
        for name in ["Search", "Chats", "Info", "Tables", "SQL", "Contacts"]:
            bmp = getattr(images, "Help" + name, None)
            if not bmp: continue # Continue for n in [..
            filename = "Help%s.png" % name
            raw = base64.b64decode(bmp.data)
            self.memoryfs["handler"].AddFile(filename, raw, wx.BITMAP_TYPE_PNG)
            self.memoryfs["files"][filename] = 1

        self.worker_detection = \
            workers.DetectDatabaseThread(self.on_detect_databases_callback)
        self.Bind(EVT_DETECTION_WORKER, self.on_detect_databases_result)
        self.Bind(EVT_OPEN_DATABASE,
                  lambda e: self.load_database_page(os.path.realpath(e.file)))

        self.Bind(wx.EVT_CLOSE, self.on_exit)
        self.Bind(wx.EVT_SIZE, self.on_size)
        self.Bind(wx.EVT_MOVE, self.on_move)
        notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_change_page)
        notebook.Bind(wx.lib.agw.flatnotebook.EVT_FLATNOTEBOOK_PAGE_CLOSING,
                      self.on_close_page)


        class FileDrop(wx.FileDropTarget):
            """A simple file drag-and-drop handler for application window."""
            def __init__(self, window):
                wx.FileDropTarget.__init__(self)
                self.window = window

            def OnDropFiles(self, x, y, filenames):
                for filename in filenames:
                    self.window.update_database_list(filename)
                for filename in filenames:
                    self.window.load_database_page(filename)

        self.notebook.DropTarget = FileDrop(self)

        self.MinSize = conf.WindowSizeMin
        if conf.WindowPosition and conf.WindowSize:
            if [-1, -1] != conf.WindowSize:
                self.Position, self.Size = conf.WindowPosition, conf.WindowSize
            else:
                self.Maximize()
        else:
            self.Center(wx.HORIZONTAL)
            self.Position.top = 50
        if self.list_db.GetItemCount() > 1:
            self.list_db.SetFocus()
        else:
            self.button_detect.SetFocus()

        self.Show(True)
        wx.CallLater(20000, self.update_check)


    def update_check(self):
        """
        Checks for an updated Skyperious version if sufficient time
        from last check has passed, and opens a dialog for upgrading
        if new version available. Schedules a new check on due date.
        """
        interval = conf.UpdateCheckInterval
        due_date = datetime.datetime.now() - interval
        if not support.update_window \
        and conf.LastUpdateCheck < due_date.strftime("%Y%m%d"):
            callback = lambda resp: self.on_check_update_callback(resp, False)
            support.check_newest_version(callback)
        elif not support.update_window:
            try:
                dt = datetime.datetime.strptime(conf.LastUpdateCheck, "%Y%m%d")
                interval = (dt + interval) - datetime.datetime.now()
            except:
                pass
        # Schedule a check for due date, should the program run that long.
        wx.CallLater(util.timedelta_seconds(interval) * 1000, self.update_check)


    def on_change_page(self, event):
        """
        Handler for changing a page in the main Notebook, remembers the visit.
        """
        p = self.notebook.GetPage(self.notebook.GetSelection())
        if not self.pages_visited or self.pages_visited[-1] != p:
            self.pages_visited.append(p)
        self.update_notebook_header()
        if event: event.Skip() # Pass event along to next handler


    def on_size(self, event):
        """Handler for window size event, tweaks controls and saves size."""
        conf.WindowSize = [-1, -1] if self.IsMaximized() else self.Size[:]
        conf.save()
        event.Skip()
        l = self.list_db
        wx.CallAfter(lambda: self and l.SetColumnWidth(0, l.Size.width - 5))


    def on_move(self, event):
        """Handler for window move event, saves position."""
        conf.WindowPosition = event.Position[:]
        conf.save()
        event.Skip()


    def update_notebook_header(self):
        """
        Removes or adds X to notebook tab style, depending on whether current
        page can be closed.
        """
        if not self:
            return
        p = self.notebook.GetPage(self.notebook.GetSelection())
        style = self.notebook.GetAGWWindowStyleFlag()
        if isinstance(p, (DatabasePage, MergerPage)):
            if p.ready_to_close \
            and not (style & wx.lib.agw.flatnotebook.FNB_X_ON_TAB):
                style |= wx.lib.agw.flatnotebook.FNB_X_ON_TAB
            elif not p.ready_to_close \
            and (style & wx.lib.agw.flatnotebook.FNB_X_ON_TAB):
                style ^= wx.lib.agw.flatnotebook.FNB_X_ON_TAB
        elif self.page_log == p:
            style |= wx.lib.agw.flatnotebook.FNB_X_ON_TAB
        elif style & wx.lib.agw.flatnotebook.FNB_X_ON_TAB: # Hide close box
            style ^= wx.lib.agw.flatnotebook.FNB_X_ON_TAB  # on main page
        if style != self.notebook.GetAGWWindowStyleFlag():
            self.notebook.SetAGWWindowStyleFlag(style)


    def create_page_main(self, notebook):
        """Creates the main page with database list and buttons."""
        page = self.page_main = wx.Panel(notebook)
        page.BackgroundColour = wx.WHITE
        notebook.AddPage(page, "Databases")
        sizer = page.Sizer = wx.BoxSizer(wx.HORIZONTAL)

        agw_style = (wx.LC_REPORT | wx.LC_NO_HEADER | wx.LC_SINGLE_SEL)
        if hasattr(wx.lib.agw.ultimatelistctrl, "ULC_USER_ROW_HEIGHT"):
            agw_style |= wx.lib.agw.ultimatelistctrl.ULC_USER_ROW_HEIGHT
        list_db = self.list_db = wx.lib.agw.ultimatelistctrl. \
            UltimateListCtrl(parent=page, agwStyle=agw_style)
        list_db.BackgroundColour = wx.Colour(236, 244, 252)
        list_db.InsertColumn(0, "")
        il = wx.ImageList(*images.ButtonHome.Bitmap.Size)
        il.Add(images.ButtonHome.Bitmap)
        il.Add(images.ButtonListDatabase.Bitmap)
        list_db.AssignImageList(il, wx.IMAGE_LIST_SMALL)
        list_db.InsertImageStringItem(0, "Home", [0])
        list_db.Select(0)
        colour = wx.NamedColour(conf.DBListBackgroundColour)
        list_db.SetItemBackgroundColour(0, colour)
        if hasattr(list_db, "SetUserLineHeight"):
            h = images.ButtonListDatabase.Bitmap.Size[1]
            list_db.SetUserLineHeight(int(h * 1.5))

        panel_commands = wx.Panel(page)
        panel_commands.Sizer = wx.BoxSizer(wx.HORIZONTAL)

        panel_main = self.panel_db_main = wx.Panel(panel_commands)
        panel_detail = self.panel_db_detail = wx.Panel(panel_commands)
        panel_main.Sizer = wx.BoxSizer(wx.VERTICAL)
        panel_detail.Sizer = wx.BoxSizer(wx.VERTICAL)

        # Create main page label and buttons
        label_main = wx.StaticText(panel_main,
                                   label="Welcome to %s" % conf.Title)
        label_main.SetForegroundColour(conf.HistoryLinkColour)
        label_main.Font = wx.Font(14, wx.FONTFAMILY_SWISS,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD, face=self.Font.FaceName)
        BUTTONS_MAIN = [
            ("opena", "&Open a database..", images.ButtonOpenA, 
             "Choose a Skype database from your computer to open."),
            ("detect", "Detect databases", images.ButtonDetect,
             "Auto-detect Skype databases from user folders."),
            ("folder", "&Import from folder.", images.ButtonFolder,
             "Select a folder where to look for Skype SQLite databases "
             "(*.db files)."),
            ("missing", "Remove missing", images.ButtonRemoveMissing,
             "Remove non-existing files from the database list."),
            ("clear", "C&lear list", images.ButtonClear,
             "Clear the current database list."), ]
        for name, label, img, note in BUTTONS_MAIN:
            button = controls.NoteButton(panel_main, label, note, img.Bitmap)
            setattr(self, "button_" + name, button)
            exec("button_%s = self.button_%s" % (name, name)) in {}, locals()
        button_missing.Hide(); button_clear.Hide()

        # Create detail page labels, values and buttons
        label_db = self.label_db = wx.TextCtrl(parent=panel_detail, value="",
            style=wx.NO_BORDER | wx.TE_MULTILINE | wx.TE_RICH)
        label_db.Font = wx.Font(12, wx.FONTFAMILY_SWISS,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD, face=self.Font.FaceName)
        label_db.BackgroundColour = panel_detail.BackgroundColour
        label_db.SetEditable(False)

        sizer_labels = wx.FlexGridSizer(cols=2, vgap=3, hgap=10)
        LABELS = [("path", "Location"), ("size", "Size"),
                  ("modified", "Last modified"), ("account", "Skype user"),
                  ("chats", "Conversations"), ("messages", "Messages")]
        for field, title in LABELS:
            lbltext = wx.StaticText(parent=panel_detail, label="%s:" % title)
            valtext = wx.TextCtrl(parent=panel_detail, value="",
                                  size=(300, -1), style=wx.NO_BORDER)
            valtext.BackgroundColour = panel_detail.BackgroundColour
            valtext.SetEditable(False)
            lbltext.ForegroundColour = wx.Colour(102, 102, 102)
            sizer_labels.Add(lbltext, border=5, flag=wx.LEFT)
            sizer_labels.Add(valtext, proportion=1, flag=wx.GROW)
            setattr(self, "label_" + field, valtext)

        BUTTONS_DETAIL = [
            ("open", "&Open", images.ButtonOpen, 
             "Open the database for searching and exploring."),
            ("compare", "Compare and &merge", images.ButtonCompare,
             "Choose another database to compare with, in order to merge "
             "their differences."),
            ("export", "&Export messages", images.ButtonExport,
             "Export all conversations from the database as HTML, TXT or CSV."),
            ("saveas", "Save &as..", images.ButtonSaveAs,
             "Save a copy of the database under another name."),
            ("remove", "Remove", images.ButtonRemove,
             "Remove this database from the list."), ]
        for name, label, img, note in BUTTONS_DETAIL:
            button = controls.NoteButton(panel_detail, label, note, img.Bitmap)
            setattr(self, "button_" + name, button)
            exec("button_%s = self.button_%s" % (name, name)) # Hack for local

        for c in list(panel_main.Children) + list(panel_detail.Children) + \
        [panel_main, panel_detail]:
           c.BackgroundColour = page.BackgroundColour 
        panel_detail.Hide()

        list_db.Bind(wx.EVT_LIST_ITEM_SELECTED,  self.on_select_list_db)
        list_db.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_open_from_list_db)
        list_db.Bind(wx.EVT_CHAR_HOOK,           self.on_list_db_key)
        button_opena.Bind(wx.EVT_BUTTON,         self.on_open_database)
        button_detect.Bind(wx.EVT_BUTTON,        self.on_detect_databases)
        button_folder.Bind(wx.EVT_BUTTON,        self.on_add_from_folder)
        button_missing.Bind(wx.EVT_BUTTON,       self.on_remove_missing)
        button_clear.Bind(wx.EVT_BUTTON,         self.on_clear_databases)
        button_open.Bind(wx.EVT_BUTTON,          self.on_open_current_database)
        button_compare.Bind(wx.EVT_BUTTON,       self.on_compare_databases)
        button_export.Bind(wx.EVT_BUTTON,        self.on_export_database)
        button_saveas.Bind(wx.EVT_BUTTON,        self.on_save_database_as)
        button_remove.Bind(wx.EVT_BUTTON,        self.on_remove_database)

        panel_main.Sizer.Add(label_main, border=10, flag=wx.ALL)
        panel_main.Sizer.Add((0, 10))
        panel_main.Sizer.Add(button_opena, flag=wx.GROW)
        panel_main.Sizer.Add(button_detect, flag=wx.GROW)
        panel_main.Sizer.Add(button_folder, flag=wx.GROW)
        panel_main.Sizer.AddStretchSpacer()
        panel_main.Sizer.Add(button_missing, flag=wx.GROW)
        panel_main.Sizer.Add(button_clear, flag=wx.GROW)
        panel_detail.Sizer.Add(label_db, border=10, flag=wx.ALL | wx.GROW)
        panel_detail.Sizer.Add(sizer_labels, border=10, flag=wx.ALL | wx.GROW)
        panel_detail.Sizer.Add((0, 10))
        panel_detail.Sizer.Add(button_open, flag=wx.GROW)
        panel_detail.Sizer.Add(button_compare, flag=wx.GROW)
        panel_detail.Sizer.Add(button_export, flag=wx.GROW)
        panel_detail.Sizer.AddStretchSpacer()
        panel_detail.Sizer.Add(button_saveas, flag=wx.GROW)
        panel_detail.Sizer.Add(button_remove, flag=wx.GROW)
        panel_commands.Sizer.Add(panel_main, proportion=1, flag=wx.GROW)
        panel_commands.Sizer.Add(panel_detail, proportion=1, flag=wx.GROW)
        sizer.Add(list_db, border=10, proportion=6, flag=wx.ALL | wx.GROW)
        sizer.Add(panel_commands, border=10, proportion=4, flag=wx.ALL | wx.GROW)
        for filename in conf.DBFiles:
            self.update_database_list(filename)
        wx.CallLater(10, page.Layout)
        wx.CallLater(150, self.SendSizeEvent)


    def create_menu(self):
        """Creates the program menu."""
        menu = wx.MenuBar()
        self.SetMenuBar(menu)

        menu_file = wx.Menu()
        menu.Append(menu_file, "&File")

        menu_open_database = self.menu_open_database = menu_file.Append(
            id=wx.NewId(), text="&Open database...\tCtrl-O",
            help="Chooses a Skype database file to open."
        )
        menu_recent = self.menu_recent = wx.Menu()
        menu_file.AppendMenu(id=wx.NewId(), text="&Recent databases",
            submenu=menu_recent, help="Recently opened databases.")
        menu_file.AppendSeparator()
        menu_exit = self.menu_exit = \
            menu_file.Append(id=wx.NewId(), text="E&xit\tAlt-X", help="Exit")

        menu_help = wx.Menu()
        menu.Append(menu_help, "&Help")

        menu_update = self.menu_update = menu_help.Append(id=wx.NewId(),
            text="Check for &updates",
            help="Checks whether a new version of %s is available" % conf.Title)
        menu_feedback = self.menu_feedback = menu_help.Append(id=wx.NewId(),
            text="Send &feedback",
            help="Sends feedback or reports errors to program author")
        menu_homepage = self.menu_homepage = menu_help.Append(id=wx.NewId(),
            text="Go to &homepage",
            help="Opens the %s homepage, %s" % (conf.Title, conf.HomeUrl))
        menu_help.AppendSeparator()
        menu_log = self.menu_log = menu_help.Append(id=wx.NewId(),
            text="Show &log window",
            help="Shows the log messages window")
        menu_console = self.menu_console = menu_help.Append(id=wx.NewId(),
            text="Sho&w Python console\tCtrl-W",
            help="Shows the Python console window")
        menu_error_reporting = self.menu_error_reporting = menu_help.Append(
            id=wx.NewId(), kind=wx.ITEM_CHECK,
            text="Automatic &error reporting",
            help="Automatically reports software errors to program author")
        menu_help.AppendSeparator()
        menu_about = self.menu_about = \
            menu_help.Append(id=wx.NewId(), text="&About %s" % conf.Title)

        self.history_file = wx.FileHistory(conf.MaxRecentFiles)
        self.history_file.UseMenu(menu_recent)
        # Reverse list, as FileHistory works like a stack
        map(self.history_file.AddFileToHistory, conf.RecentFiles[::-1])
        wx.EVT_MENU_RANGE(self, wx.ID_FILE1, wx.ID_FILE9, self.on_recent_file)
        menu_error_reporting.Check(conf.ErrorReportsAutomatic)

        self.Bind(wx.EVT_MENU, self.on_open_database, menu_open_database)
        self.Bind(wx.EVT_MENU, self.on_exit, menu_exit)
        self.Bind(wx.EVT_MENU, self.on_check_update, menu_update)
        self.Bind(wx.EVT_MENU, self.on_open_feedback, menu_feedback)
        self.Bind(wx.EVT_MENU, self.on_menu_homepage, menu_homepage)
        self.Bind(wx.EVT_MENU, self.on_showhide_log, menu_log)
        self.Bind(wx.EVT_MENU, self.on_showhide_console, menu_console)
        self.Bind(wx.EVT_MENU, self.on_toggle_error_reporting,
                  menu_error_reporting)
        self.Bind(wx.EVT_MENU, self.on_about, menu_about)


    def on_toggle_error_reporting(self, event):
        """Handler for toggling automatic error reporting."""
        conf.ErrorReportsAutomatic = event.IsChecked()
        conf.save()


    def on_list_db_key(self, event):
        """
        Handler for pressing a key in dblist, loads selected database on Enter.
        """
        if self.list_db.GetFirstSelected() > 0 and not event.AltDown() \
        and event.KeyCode in [wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER]:
            self.load_database_page(self.db_filename)
        elif event.KeyCode in [wx.WXK_DELETE] and self.db_filename:
            self.on_remove_database(None)

        event.Skip()


    def on_open_feedback(self, event):
        """Handler for clicking to send feedback, opens the feedback form."""
        if support.feedback_window:
            if not support.feedback_window.Shown:
                support.feedback_window.Show()
            support.feedback_window.Raise()
        else:
            support.feedback_window = support.FeedbackDialog(self)


    def on_menu_homepage(self, event):
        """Handler for opening Skyperious webpage from menu,"""
        webbrowser.open(conf.HomeUrl)


    def on_about(self, event):
        """
        Handler for clicking "About Skyperious" menu, opens a small info frame.
        """
        text = step.Template(templates.ABOUT_TEXT).expand()
        AboutDialog(self, text).ShowModal()


    def on_check_update(self, event):
        """
        Handler for checking for updates, starts a background process for
        checking for and downloading the newest version.
        """
        if not support.update_window:
            main.status("Checking for new version of %s.", conf.Title)
            wx.CallAfter(support.check_newest_version,
                         self.on_check_update_callback)
        elif hasattr(support.update_window, "Raise"):
            support.update_window.Raise()


    def on_check_update_callback(self, check_result, full_response=True):
        """
        Callback function for processing update check result, offers new
        version for download if available.

        @param   full_response  if False, show message only if update available
        """
        if not self:
            return
        support.update_window = True
        main.status("")
        if check_result:
            version, url, changes = check_result
            MAX = 1500
            changes = changes[:MAX] + ".." if len(changes) > MAX else changes
            main.status_flash("New %s version %s available.",
                              conf.Title, version)
            if wx.OK == wx.MessageBox(
                "Newer version (%s) available. You are currently on "
                "version %s.%s\nDownload and install %s %s?" %
                (version, conf.Version, "\n\n%s\n" % changes, conf.Title, version),
                "Update information", wx.OK | wx.CANCEL | wx.ICON_INFORMATION
            ):
                wx.CallAfter(support.download_and_install, url)
        elif full_response and check_result is not None:
            wx.MessageBox(
                "You are using the latest version of %s." % conf.Title,
                "Update information", wx.OK | wx.ICON_INFORMATION)
        elif full_response:
            wx.MessageBox("Could not contact download server.",
                          "Update information", wx.OK | wx.ICON_WARNING)
        if check_result is not None:
            conf.LastUpdateCheck = datetime.date.today().strftime("%Y%m%d")
            conf.save()
        support.update_window = None


    def on_detect_databases(self, event):
        """
        Handler for clicking to auto-detect Skype databases, starts the
        detection in a background thread.
        """
        if self.button_detect.FindFocus() == self.button_detect:
            self.list_db.SetFocus()
        main.logstatus("Searching local computer for Skype databases..")
        self.button_detect.Enabled = False
        self.worker_detection.work(True)


    def on_detect_databases_callback(self, result):
        """Callback for DetectDatabaseThread, posts the data to self."""
        if self: # Check if instance is still valid (i.e. not destroyed by wx)
            wx.PostEvent(self, DetectionWorkerEvent(result=result))


    def on_detect_databases_result(self, event):
        """
        Handler for getting results from database detection thread, adds the
        results to the database list.
        """
        result = event.result
        if "filenames" in result:
            for f in result["filenames"]:
                if self.update_database_list(f):
                    main.log("Detected Skype database %s." % f)
        if "count" in result:
            main.logstatus_flash("Detected %d%s Skype database%s.",
                result["count"],
                " additional" if not (result["count"]) else "",
                "" if result["count"] == 1 else "s"
            )
        if result.get("done", False):
            self.button_detect.Enabled = True


    def update_database_list(self, filename=""):
        """
        Inserts the database into the list, if not there already, and updates
        UI buttons.

        @param   filename  possibly new filename, if any
        @return            True if was file was new or changed, False otherwise
        """
        result, count_initial = False, self.list_db.GetItemCount() - 1
        # Insert into database lists, if not already there
        if filename:
            filename = util.to_unicode(filename)
            if filename not in conf.DBFiles:
                conf.DBFiles.append(filename)
                conf.save()
            data = collections.defaultdict(lambda: None)
            if os.path.exists(filename):
                data["size"] = os.path.getsize(filename)
                data["last_modified"] = datetime.datetime.fromtimestamp(
                                        os.path.getmtime(filename))
            data_old = self.db_filenames.get(filename)
            if not data_old or data_old["size"] != data["size"] \
            or data_old["last_modified"] != data["last_modified"]:
                if filename not in self.db_filenames:
                    self.db_filenames[filename] = data
                    idx = self.list_db.GetItemCount()
                    self.list_db.InsertImageStringItem(idx, filename, [1])
                    colour = wx.NamedColour(conf.DBListBackgroundColour)
                    self.list_db.SetItemBackgroundColour(idx, colour)
                    # self is not shown: form creation time, reselect last file
                    if not self.Shown and filename in conf.LastSelectedFiles:
                        self.list_db.Select(idx)
                result = True

        self.button_missing.Shown = (self.list_db.GetItemCount() > 1)
        self.button_clear.Shown = (self.list_db.GetItemCount() > 1)
        if self.Shown:
            self.list_db.SetColumnWidth(0, self.list_db.Size.width - 5)
        if result and not count_initial:
            self.page_main.SendSizeEvent()
            wx.CallAfter(self.page_main.Layout)
        return result


    def on_clear_databases(self, event):
        """Handler for clicking to clear the database list."""
        if (self.list_db.GetItemCount() > 1) and wx.OK == wx.MessageBox(
            "Are you sure you want to clear the list of all databases?",
            conf.Title, wx.OK | wx.CANCEL | wx.ICON_QUESTION
        ):
            while self.list_db.GetItemCount() > 1:
                self.list_db.DeleteItem(1)
            del conf.DBFiles[:]
            del conf.LastSelectedFiles[:]
            del conf.RecentFiles[:]
            conf.LastSearchResults.clear()
            while self.history_file.Count:
                self.history_file.RemoveFileFromHistory(0)
            self.db_filenames.clear()
            conf.save()
            self.update_database_list()


    def on_save_database_as(self, event):
        """Handler for clicking to save a copy of a database in the list."""
        original = self.db_filename
        if not os.path.exists(original):
            wx.MessageBox(
                "The file \"%s\" does not exist on this computer." % original,
                conf.Title, wx.OK | wx.ICON_INFORMATION
            )
            return

        dialog = wx.FileDialog(parent=self, message="Save a copy..",
            defaultDir=os.path.split(original)[0],
            defaultFile=os.path.basename(original),
            style=wx.FD_OVERWRITE_PROMPT | wx.FD_SAVE | wx.RESIZE_BORDER
        )
        if wx.ID_OK == dialog.ShowModal():
            wx.GetApp().Yield(True) # Allow UI to refresh
            newpath = dialog.GetPath()
            success = False
            try:
                shutil.copyfile(original, newpath)
                success = True
            except Exception, e:
                main.log("%s when trying to copy %s to %s.",
                    e, original, newpath
                )
                if self.skype_handler and self.skype_handler.is_running():
                    response = wx.MessageBox(
                        "Could not save a copy of \"%s\" as \"%s\".\n\n"
                        "Probably because Skype is running. "
                        "Close Skype and try again?" % (original, newpath),
                        conf.Title, wx.OK | wx.CANCEL | wx.ICON_QUESTION)
                    if wx.OK == response:
                        self.skype_handler.shutdown()
                        success, _ = util.try_until(
                            lambda: shutil.copyfile(original, newpath)
                        )
                        if not success:
                            wx.MessageBox(
                                "Still could not copy \"%s\" to \"%s\"." %
                                (original, newpath),
                                conf.Title, wx.OK | wx.ICON_WARNING)
                else:
                    wx.MessageBox(
                        "Failed to copy \"%s\" to \"%s\"." % (original, newpath),
                        conf.Title, wx.OK | wx.ICON_WARNING
                    )
            if success:
                main.logstatus_flash("Saved a copy of %s as %s.",
                                     original, newpath)
                self.update_database_list(newpath)


    def on_remove_database(self, event):
        """Handler for clicking to remove an item from the database list."""
        filename = self.db_filename
        if filename and wx.OK == wx.MessageBox(
            "Remove %s from database list?" % filename,
            conf.Title, wx.OK | wx.CANCEL | wx.ICON_QUESTION
        ):
            if filename in conf.DBFiles:
                conf.DBFiles.remove(filename)
            if filename in conf.LastSelectedFiles:
                conf.LastSelectedFiles.remove(filename)
            if filename in conf.LastSearchResults:
                del conf.LastSearchResults[filename]
            if filename in self.db_filenames:
                del self.db_filenames[filename]
            for i in range(self.list_db.GetItemCount()):
                if self.list_db.GetItemText(i) == filename:
                    self.list_db.DeleteItem(i)
                    break # break for i in range(self.list_db..
            self.db_filename = None
            self.list_db.Select(0)
            self.update_database_list()
            conf.save()


    def on_remove_missing(self, event):
        """Handler to remove nonexistent files from the database list."""
        selecteds = range(1, self.list_db.GetItemCount())
        filter_func = lambda i: not os.path.exists(self.list_db.GetItemText(i))
        selecteds = filter(filter_func, selecteds)
        for i in range(len(selecteds)):
            # - i, as item count is getting smaller one by one
            selected = selecteds[i] - i
            filename = self.list_db.GetItemText(selected)
            if filename in conf.DBFiles:
                conf.DBFiles.remove(filename)
            if filename in conf.LastSelectedFiles:
                conf.LastSelectedFiles.remove(filename)
            if filename in self.db_filenames:
                del self.db_filenames[filename]
            self.list_db.DeleteItem(selected)
        conf.save()
        self.update_database_list()


    def on_showhide_log(self, event):
        """Handler for clicking to show/hide the log window."""
        if self.notebook.GetPageIndex(self.page_log) < 0:
            self.notebook.AddPage(self.page_log, "Log")
            self.page_log.is_hidden = False
            self.page_log.Show()
            self.notebook.SetSelection(self.notebook.GetPageCount() - 1)
            self.on_change_page(None)
        else:
            self.page_log.is_hidden = True
            self.notebook.RemovePage(self.notebook.GetPageIndex(self.page_log))
        label = "Show" if self.page_log.is_hidden else "Hide"
        self.menu_log.Text = "%s &log window" % label
        self.menu_log.Help = "%ss the log messages window" % label


    def on_export_database(self, event):
        """
        Handler for clicking to export a whole database, lets the user
        specify a directory where to save chat files and exports all chats.
        """
        self.dialog_savefile.Filename = "Filename will be ignored"
        self.dialog_savefile.Message = "Choose folder where to save all chats"
        self.dialog_savefile.Wildcard = \
            "HTML document (*.html)|*.html|" \
            "Text document (*.txt)|*.txt|" \
            "CSV spreadsheet (*.csv)|*.csv"
        if wx.ID_OK == self.dialog_savefile.ShowModal():
            self.button_export.Enabled = False
            count, error, errormsg = 0, False, None
            dirname = os.path.dirname(self.dialog_savefile.GetPath())
            extname = ["html", "txt", "csv"][self.dialog_savefile.FilterIndex]
            db = self.load_database(self.db_filename)
            if not db:
                error = True
            elif "conversations" not in db.tables:
                error = True
                errormsg = "Cannot export %s. Not a valid Skype database?" % (db)
            if not error:
                d = "Export from %s" % (db.id or os.path.basename(db.filename))
                db_dirname = util.unique_path(os.path.join(dirname, d))
                try:
                    os.mkdir(db_dirname)
                except Exception, e:
                    errormsg = "Failed to create directory %s: %s" % (db_dirname, e)
                    error = True
            if not error:
                chats = db.get_conversations()
                busy = controls.BusyPanel(
                    self, "Exporting all %s from \"%s\"\nas %s\nunder %s." %
                    (util.plural("chat", chats), db.filename,
                    extname.upper(), db_dirname))
                main.logstatus("Exporting all %s from %s as %s under %s.",
                               util.plural("chat", chats),
                               db.filename, extname.upper(), db_dirname)
                wx.GetApp().Yield(True) # Allow UI to refresh
                if not db.has_consumers():
                    db.get_conversations_stats(chats)
                try:
                    for chat in chats:
                        if chat["message_count"]:
                            main.status("Exporting %s", chat["title_long_lc"])
                            wx.GetApp().Yield(True) # Allow status to refresh
                            f = "Skype %s.%s" % (chat["title_long_lc"], extname)
                            f = os.path.join(db_dirname, util.safe_filename(f))
                            f = util.unique_path(f)
                            messages = db.get_messages(chat)
                            if export.export_chat(chat, messages, f, db):
                                count += 1
                            else:
                                e = "An error occurred when saving \"%s\"." % f
                                error, errormsg = True, e
                                break # break for chat in chats
                        else:
                            main.logstatus("Skipping %s: no messages.",
                                           chat["title_long_lc"])
                except Exception, e:
                    errormsg = ("An unexpected error occurred when saving "
                                "all %s from \"%s\" as %s under %s: %s" %
                                (util.plural("chat", chats),
                                 extname.upper(), db_dirname, e))
                    error = True
                busy.Close()
            self.button_export.Enabled = True
            if not error:
                main.logstatus_flash("Exported %s from %s as %s "
                    "under %s.", util.plural("chat", count), db.filename,
                    extname.upper(), db_dirname)
            elif errormsg:
                main.logstatus_flash(
                    "Failed to export all chats from %s as %s.",
                    db.filename, extname.upper())
                wx.MessageBox(errormsg, conf.Title, wx.OK | wx.ICON_WARNING)
            if db and not db.has_consumers():
                del self.dbs[db.filename]
                db.close()
            if db and not error:
                util.start_file(db_dirname)


    def on_compare_menu(self, event):
        """
        Handler for choosing a file from the compare popup menu, loads both
        databases and opens the merger page.
        """
        filename1, filename2 = self.db_filename, None
        db1, db2 = None, None
        # Find menuitem index and label from original menu by event ID
        indexitems = enumerate(event.EventObject.GetMenuItems())
        indexitem = [(i, m) for i, m in indexitems if m.GetId() == event.Id]
        i, item = indexitem[0] if indexitem else (-1, None)
        if i > 0 and item:
            filename2 = item.GetLabel()
        elif not i: # First menu item: open a file from computer
            dialog = wx.FileDialog(
                parent=self, message="Open", defaultFile="",
                wildcard="Skype database (*.db)|*.db|All files|*.*",
                style=wx.FD_FILE_MUST_EXIST | wx.FD_OPEN | wx.RESIZE_BORDER)
            dialog.ShowModal()
            filename2 = dialog.GetPath()
        if filename1 == filename2:
            wx.MessageBox("Cannot compare %s with itself." % (filename1),
                          conf.Title, wx.OK | wx.ICON_WARNING)
            filename2 = None
        self.compare_databases(filename1, filename2)


    def compare_databases(self, filename1, filename2):
        """Opens the two databases for comparison, if possible."""
        db1, db2, page = None, None, None
        if filename1 and filename2:
            db1 =  self.load_database(filename1)
        if db1:
            db2 = self.load_database(filename2)
        if db1 and db2:
            dbset = set((db1, db2))
            pp = filter(lambda i: i and set([i.db1, i.db2]) == dbset,
                        self.merger_pages)
            page = pp[0] if pp else None
            if not page:
                f1, f2 = filename1, filename2
                page = MergerPage(self.notebook, db1, db2,
                       self.get_unique_tab_title("Database comparison"))
                self.merger_pages[page] = (db1, db2)
                self.UpdateAccelerators()
        elif db1 or db2:
            # Close DB with no owner
            for db in filter(None, [db1, db2]):
                if not db.has_consumers():
                    main.log("Closed database %s." % db.filename)
                    del self.dbs[db.filename]
                    db.close()
        if page:
            for i in range(self.notebook.GetPageCount()):
                if self.notebook.GetPage(i) == page:
                    self.notebook.SetSelection(i)
                    self.update_notebook_header()
                    break # break for i in range(self.notebook..


    def on_compare_databases(self, event):
        """
        Handler for clicking to compare a selected database with another, shows
        a popup menu for choosing the second database file.
        """
        menu = wx.lib.agw.flatmenu.FlatMenu()
        item = wx.lib.agw.flatmenu.FlatMenuItem(menu, wx.NewId(),
               "Select a file from your computer..")
        menu.AppendItem(item)
        recents = [f for f in conf.RecentFiles if f != self.db_filename][:5]
        others = [f for f in conf.DBFiles
                  if f not in recents and f != self.db_filename]
        if recents or others:
            menu.AppendSeparator()
        if recents:
            item = wx.lib.agw.flatmenu.FlatMenuItem(menu, wx.NewId(),
                                                    "Recent files")
            item.Enable(False)
            menu.AppendItem(item)
            for f in recents:
                i = wx.lib.agw.flatmenu.FlatMenuItem(menu, wx.NewId(), f)
                menu.AppendItem(i)
            if others:
                menu.AppendSeparator()
                item = wx.lib.agw.flatmenu.FlatMenuItem(menu, wx.NewId(),
                                                        "Rest of list")
                item.Enable(False)
                menu.AppendItem(item)
        for f in sorted(others):
            item = wx.lib.agw.flatmenu.FlatMenuItem(menu, wx.NewId(), f)
            menu.AppendItem(item)
        for item in menu.GetMenuItems():
            self.Bind(wx.EVT_MENU, self.on_compare_menu, item)

        sz_btn, pt_btn = event.EventObject.Size, event.EventObject.Position
        pt_btn = event.EventObject.Parent.ClientToScreen(pt_btn)
        menu.SetOwnerHeight(sz_btn.y)
        if menu.Size.width < sz_btn.width:
            menu.Size = sz_btn.width, menu.Size.height
        menu.Popup(pt_btn, self)


    def on_open_database(self, event):
        """
        Handler for open database menu or button, displays a file dialog and
        loads the chosen database.
        """
        dialog = wx.FileDialog(
            parent=self, message="Open", defaultFile="",
            wildcard="Skype database (*.db)|*.db|All files|*.*",
            style=wx.FD_FILE_MUST_EXIST | wx.FD_OPEN | wx.RESIZE_BORDER
        )
        dialog.ShowModal()
        filename = dialog.GetPath()
        if filename:
            self.update_database_list(filename)
            self.load_database_page(filename)


    def on_recent_file(self, event):
        """Handler for clicking an entry in Recent Files menu."""
        filename = self.history_file.GetHistoryFile(event.GetId() - wx.ID_FILE1)
        self.update_database_list(filename)
        self.load_database_page(filename)


    def on_add_from_folder(self, event):
        """
        Handler for clicking to select folder where to search Skype databases,
        adds found databases to database lists.
        """
        if self.dialog_selectfolder.ShowModal() == wx.ID_OK:
            folder = self.dialog_selectfolder.GetPath()
            main.logstatus("Detecting Skype databases under %s.", folder)
            count = 0
            for filename in skypedata.find_databases(folder):
                if filename not in self.db_filenames:
                    main.log("Detected Skype database %s.", filename)
                    self.update_database_list(filename)
                    count += 1
            main.logstatus_flash("Detected %s under %s.",
                util.plural("new Skype database", count), folder)


    def on_open_current_database(self, event):
        """Handler for clicking to open selected files from database list."""
        if self.db_filename:
            self.load_database_page(self.db_filename)


    def on_open_from_list_db(self, event):
        """Handler for clicking to open selected files from database list."""
        if event.GetIndex() > 0:
            self.load_database_page(event.GetText())



    def update_database_stats(self, filename):
        """Opens the database and updates main page UI with database info."""
        db = None
        try:
            db = self.dbs.get(filename) or skypedata.SkypeDatabase(filename)
        except Exception, e:
            self.label_account.Value = "(database not readable)"
            self.label_messages.Value = "Error text: %s" % e
            self.label_account.ForegroundColour = conf.LabelErrorColour 
            self.label_chats.ForegroundColour = conf.LabelErrorColour
            main.log("Error opening %s.\n\n%s", filename,
                     traceback.format_exc())
            return
        try:
            if db.account:
                text = "%(name)s (%(skypename)s)" % db.account
                self.label_account.Value = text
            stats = db.execute("SELECT COUNT(*) AS count FROM Conversations")
            text = "%(count)s" % stats.next()
            stats = db.execute("SELECT type,"
                "COALESCE(NULLIF(displayname, ''), NULLIF(meta_topic, '')) "
                "AS title FROM Conversations WHERE displayname IS NOT NULL "
                "ORDER by last_activity_timestamp desc limit 1")
            chat = next(stats, None)
            if chat:
                title = ("with %s" if skypedata.CHATS_TYPE_SINGLE == chat["type"]
                         else "\"%s\"") % chat["title"]
                text += ", latest %s" % title
            self.label_chats.Value = text
            stats = db.execute("SELECT MAX(timestamp) AS dt, NULL as date,"
                               "COUNT(*) AS count FROM Messages").next()
            if stats["dt"] is not None:
                dt = datetime.datetime.fromtimestamp(stats["dt"])
                stats["date"] = dt.strftime("%Y-%m-%d %H:%M")
            self.label_messages.Value = "%(count)s, last at %(date)s" % stats
            data = self.db_filenames.get(filename, {})
            data["account"] = self.label_account.Value
            data["chats"] = self.label_chats.Value
            data["messages"] = self.label_messages.Value
        except Exception, e:
            if not self.label_account.Value:
                self.label_account.Value = "(not recognized as a Skype database)"
                self.label_account.ForegroundColour = conf.LabelErrorColour
            self.label_chats.Value = "Error text: %s" % e
            self.label_chats.ForegroundColour = conf.LabelErrorColour
            main.log("Error loading data from %s.\n\n%s", filename,
                     traceback.format_exc())


    def on_select_list_db(self, event):
        """Handler for selecting an item in main list, updates info panel."""
        if event.GetIndex() > 0 \
        and event.GetText() != self.db_filename:
            filename = self.db_filename = event.GetText()
            path, tail = os.path.split(filename)
            self.label_db.Value = tail
            self.label_path.Value = path
            self.label_size.Value = self.label_modified.Value = ""
            self.label_account.Value = self.label_chats.Value = ""
            self.label_messages.Value = ""
            self.label_account.ForegroundColour = self.ForegroundColour
            self.label_size.ForegroundColour = self.ForegroundColour
            self.label_chats.ForegroundColour = self.ForegroundColour
            if not self.panel_db_detail.Shown:
                self.panel_db_main.Hide()
                self.panel_db_detail.Show()
                self.panel_db_detail.ContainingSizer.Layout()
                wx.CallAfter(self.panel_db_main.ContainingSizer.Layout)
            if os.path.exists(filename):
                sz = os.path.getsize(filename)
                dt = datetime.datetime.fromtimestamp(os.path.getmtime(filename))
                self.label_size.Value = util.format_bytes(sz)
                self.label_modified.Value = dt.strftime("%Y-%m-%d %H:%M:%S")
                data = self.db_filenames[filename]
                if data["size"] == sz and data["last_modified"] == dt \
                and data["messages"]:
                    # File does not seem changed: use cached values
                    self.label_account.Value = data["account"]
                    self.label_chats.Value = data["chats"]
                    self.label_messages.Value = data["messages"]
                else:
                    wx.CallLater(10, self.update_database_stats, filename)
            else:
                self.label_size.Value = "File does not exist."
                self.label_size.ForegroundColour = conf.LabelErrorColour
        elif event.GetIndex() == 0 and not self.panel_db_main.Shown:
            self.db_filename = None
            self.panel_db_main.Show()
            self.panel_db_detail.Hide()
            self.panel_db_main.ContainingSizer.Layout()
            wx.CallAfter(self.panel_db_main.ContainingSizer.Layout)


    def on_exit(self, event):
        """
        Handler on application exit, asks about unsaved changes, if any.
        """
        do_exit = True
        unsaved_dbs = {} # {SkypeDatabase: filename, }
        merging_pages = [] # [MergerPage title, ]
        for db in self.db_pages.values():
            if db.get_unsaved_grids():
                unsaved_dbs[db] = db.filename
        if unsaved_dbs:
            response = wx.MessageBox(
                "There are unsaved changes in data grids\n(%s). "
                "Save changes before closing?" % (
                    "\n".join(textwrap.wrap(", ".join(unsaved_dbs.values())))
                 ),
                 conf.Title, wx.YES | wx.NO | wx.CANCEL | wx.ICON_QUESTION
            )
            if wx.YES == response:
                for db in unsaved_dbs:
                    db.save_unsaved_grids()
            do_exit = (wx.CANCEL != response)
        if do_exit:
            merging_pages = filter(lambda x: x.is_merging, self.merger_pages)
            merging_pages = [p.Label for p in merging_pages]
        if merging_pages:
            response = wx.MessageBox(
                "Merging is currently in progress in %s.\nExit anyway? "
                "This can result in corrupt data." % 
                "\n".join(textwrap.wrap(", ".join(merging_pages))),
                conf.Title, wx.OK | wx.CANCEL | wx.ICON_QUESTION)
            do_exit = (wx.CANCEL != response)
        if do_exit:
            for page in self.db_pages:
                # Save search box state
                if conf.SearchHistory[-1:] == [""]: # Clear empty search flag
                    conf.SearchHistory = conf.SearchHistory[:-1]
                util.add_unique(conf.SearchHistory, page.edit_searchall.Value,
                                1, conf.SearchHistoryMax)

                active_idx = page.notebook.Selection
                if active_idx:
                    conf.LastActivePage[page.db.filename] = active_idx
                elif page.db.filename in conf.LastActivePage:
                    del conf.LastActivePage[page.db.filename]

                # Save last search results HTML
                search_data = page.html_searchall.GetActiveTabData()
                if search_data:
                    info = {}
                    if search_data.get("info"):
                        info["map"] = search_data["info"].get("map")
                        info["text"] = search_data["info"].get("text")
                    data = {"content": search_data["content"],
                            "id": search_data["id"], "info": info,
                            "title": search_data["title"], }
                    conf.LastSearchResults[page.db.filename] = data
                elif page.db.filename in conf.LastSearchResults:
                    del conf.LastSearchResults[page.db.filename]

                # Save page SQL window content, if changed from previous value
                sql_text = page.stc_sql.Text
                if sql_text != conf.SQLWindowTexts.get(page.db.filename, ""):
                    if sql_text:
                        conf.SQLWindowTexts[page.db.filename] = sql_text
                    elif page.db.filename in conf.SQLWindowTexts:
                        del conf.SQLWindowTexts[page.db.filename]

            # Save last selected files in db lists, to reselect them on rerun
            del conf.LastSelectedFiles[:]
            selected = self.list_db.GetFirstSelected()
            while selected > 0:
                filename = self.list_db.GetItemText(selected)
                conf.LastSelectedFiles.append(filename)
                selected = self.list_db.GetNextSelected(selected)
            conf.WindowPosition = self.Position[:]
            conf.WindowSize = [-1, -1] if self.IsMaximized() else self.Size[:]
            conf.save()
            self.Destroy()


    def on_close_page(self, event):
        """
        Handler for closing a page, asks the user about saving unsaved data,
        if any, removes page from main notebook and updates accelerators.
        """
        if event.EventObject == self.notebook:
            page = self.notebook.GetPage(event.GetSelection())
        else:
            page = event.EventObject
            page.Show(False)
        if self.page_log == page:
            if not self.page_log.is_hidden:
                event.Veto() # Veto delete event
                self.on_showhide_log(None) # Fire remove event
            self.pages_visited = filter(lambda x: x != page, self.pages_visited)
            self.page_log.Show(False)
            return
        elif not isinstance(page, (DatabasePage, MergerPage)) \
        or not page.ready_to_close:
            return event.Veto()

        # Remove page from MainWindow data structures
        if isinstance(page, DatabasePage):
            do_close = True
            unsaved = page.db.get_unsaved_grids()
            if unsaved:
                response = wx.MessageBox(
                    "Some tables in %s have unsaved data (%s).\n\n"
                    "Save changes before closing?" % (
                        page.db, ", ".join(unsaved)
                    ),
                    conf.Title, wx.YES | wx.NO | wx.CANCEL | wx.ICON_QUESTION
                )
                if wx.YES == response:
                    page.db.save_unsaved_grids()
                elif wx.CANCEL == response:
                    do_close = False
            if not do_close:
                return event.Veto()

            if page.notebook.Selection:
                conf.LastActivePage[page.db.filename] = page.notebook.Selection
            elif page.db.filename in conf.LastActivePage:
                del conf.LastActivePage[page.db.filename]

            [i.stop() for i in page.workers_search.values()]
            # Save search box state
            if conf.SearchHistory[-1:] == [""]: # Clear empty search flag
                conf.SearchHistory = conf.SearchHistory[:-1]
            search_value = page.edit_searchall.Value
            util.add_unique(conf.SearchHistory, search_value, 1,
                            conf.SearchHistoryMax)

            # Save last search results HTML
            search_data = page.html_searchall.GetActiveTabData()
            if search_data:
                info = {}
                if search_data.get("info"):
                    info["map"] = search_data["info"].get("map")
                    info["text"] = search_data["info"].get("text")
                data = {"content": search_data["content"],
                        "id": search_data["id"], "info": info,
                        "title": search_data["title"], }
                conf.LastSearchResults[page.db.filename] = data
            elif page.db.filename in conf.LastSearchResults:
                del conf.LastSearchResults[page.db.filename]

            # Save page SQL window content, if changed from previous value
            sql_text = page.stc_sql.Text
            if sql_text != conf.SQLWindowTexts.get(page.db.filename, ""):
                if sql_text:
                    conf.SQLWindowTexts[page.db.filename] = sql_text
                elif page.db.filename in conf.SQLWindowTexts:
                    del conf.SQLWindowTexts[page.db.filename]

            if page in self.db_pages:
                del self.db_pages[page]
            page_dbs = [page.db]
            main.log("Closed database tab for %s." % page.db)
            conf.save()
        else:
            if page.is_merging:
                response = wx.MessageBox(
                    "Merging is currently in progress in %s.\nClose anyway? "
                    "This can result in corrupt data." % page.Label,
                    conf.Title, wx.OK | wx.CANCEL | wx.ICON_QUESTION)
                if wx.CANCEL == response:
                    return event.Veto()

            if page in self.merger_pages:
                del self.merger_pages[page]
            page_dbs = [page.db1, page.db2]
            page.worker_merge.stop()
            page.worker_merge.join()
            main.log("Closed comparison tab for %s and %s.",
                     page.db1, page.db2)

        # Close databases, if not used in any other page
        for db in page_dbs:
            db.unregister_consumer(page)
            if not db.has_consumers():
                if db.filename in self.dbs:
                    del self.dbs[db.filename]
                db.close()
                main.log("Closed database %s." % db)
        # Remove any dangling references
        if self.page_merge_latest == page:
            self.page_merge_latest = None
        if self.page_db_latest == page:
            self.page_db_latest = None
        self.SendSizeEvent() # Multiline wx.Notebooks need redrawing
        self.UpdateAccelerators() # Remove page accelerators

        # Remove page from visited pages order
        self.pages_visited = filter(lambda x: x != page, self.pages_visited)
        index_new = 0
        if self.pages_visited:
            for i in range(self.notebook.GetPageCount()):
                if self.notebook.GetPage(i) == self.pages_visited[-1]:
                    index_new = i
                    break
        self.notebook.SetSelection(index_new)


    def on_clear_searchall(self, event):
        """
        Handler for clicking to clear search history in a database page,
        confirms action and clears history globally.
        """
        if wx.OK == wx.MessageBox("Clear search history?",
            conf.Title, wx.OK | wx.CANCEL | wx.ICON_WARNING
        ):
            conf.SearchHistory = []
            for page in self.db_pages:
                page.edit_searchall.SetChoices(conf.SearchHistory)
                page.edit_searchall.ShowDropDown(False)
            conf.save()


    def get_unique_tab_title(self, title):
        """
        Returns a title that is unique for the current notebook - if the
        specified title already exists, appends a counter to the end,
        e.g. "Database comparison (1)". Title is shortened from the left
        if longer than allowed.
        """
        if len(title) > conf.MaxTabTitleLength:
            title = "..%s" % title[-conf.MaxTabTitleLength:]
        unique = title_base = title
        all_titles = [self.notebook.GetPageText(i)
                      for i in range(self.notebook.GetPageCount())]
        i = 1 # Start counter from 1
        while unique in all_titles:
            unique = "%s (%d)" % (title_base, i)
            i += 1
        return unique


    def load_database(self, filename):
        """
        Tries to load the specified database, if not already open, and returns
        it.
        """
        db = self.dbs.get(filename)
        if not db:
            db = None
            if os.path.exists(filename):
                try:
                    db = skypedata.SkypeDatabase(filename)
                except:
                    is_accessible = False
                    try:
                        with open(filename, "rb") as f:
                            is_accessible = True
                    except Exception, e:
                        pass
                    if not is_accessible and self.skype_handler \
                    and self.skype_handler.is_running():
                        #wx.GetApp().Yield(True) # Allow UI to refresh
                        response = wx.MessageBox(
                            "Could not open %s.\n\n"
                            "Probably because Skype is running. "
                            "Close Skype and try again?" % filename,
                            conf.Title, wx.OK | wx.CANCEL | wx.ICON_WARNING
                        )
                        if wx.OK == response:
                            self.skype_handler.shutdown()
                            try_result, db = util.try_until(lambda:
                                skypedata.SkypeDatabase(filename, False)
                            )
                            if not try_result:
                                wx.MessageBox(
                                    "Still could not open %s." % filename,
                                    conf.Title, wx.OK | wx.ICON_WARNING
                                )
                    elif not is_accessible:
                        wx.MessageBox(
                            "Could not open %s.\n\n"
                            "Some other process may be using the file."
                            % filename, conf.Title, wx.OK | wx.ICON_WARNING
                        )
                    else:
                        wx.MessageBox(
                            "Could not open %s.\n\n"
                            "Not a valid SQLITE database?" % filename,
                            conf.Title, wx.OK | wx.ICON_WARNING
                        )
                if db:
                    main.log("Opened %s (%s).", db, util.format_bytes(
                        db.filesize
                    ))
                    main.status_flash("Reading Skype database file %s.", db)
                    self.dbs[filename] = db
                    # Add filename to Recent Files menu and conf, if needed
                    if filename in conf.RecentFiles:
                        idx = conf.RecentFiles.index(filename)
                        self.history_file.RemoveFileFromHistory(idx)
                    self.history_file.AddFileToHistory(filename)
                    util.add_unique(conf.RecentFiles, filename, -1,
                                    conf.MaxRecentFiles)
                    conf.save()
                    self.check_future_dates(db)
            else:
                wx.MessageBox("Nonexistent file: %s." % filename,
                              conf.Title, wx.OK | wx.ICON_WARNING)
        return db


    def load_database_page(self, filename):
        """
        Tries to load the specified database, if not already open, create a
        subpage for it, if not already created, and focuses the subpage.
        """
        db = None
        page = None
        if filename in self.dbs:
            db = self.dbs[filename]
        if db and db in self.db_pages.values():
            pp = filter(lambda i: i and (i.db == db), self.db_pages)
            page = pp[0] if pp else None
        if not page:
            if not db:
                db = self.load_database(filename)
            if db:
                main.status_flash("Opening Skype database file %s." % db)
                tab_title = self.get_unique_tab_title(db.filename)
                page = DatabasePage(self.notebook, tab_title, db,
                                    self.memoryfs, self.skype_handler)
                self.db_pages[page] = db
                self.UpdateAccelerators()
                self.Bind(wx.EVT_LIST_DELETE_ALL_ITEMS,
                          self.on_clear_searchall, page.edit_searchall)
        if page:
            for i in range(self.notebook.GetPageCount()):
                if self.notebook.GetPage(i) == page:
                    self.notebook.SetSelection(i)
                    self.update_notebook_header()
                    break


    def check_future_dates(self, db):
        """
        Checks the database for messages with a future date and asks the user
        about fixing them.
        """
        future_count, max_datetime = db.check_future_dates()
        if future_count:
            delta = datetime.datetime.now() - max_datetime
            dialog = DayHourDialog(parent=self,
                message="The database has %s with a "
                "future timestamp (last being %s).\nThis can "
                "happen if the computer\"s clock has been set "
                "to a future date when the messages were "
                "received.\n\n"
                "If you want to fix these messages, "
                "enter how many days/hours to move them:" %
                  (util.plural("message", future_count), max_datetime),
                caption=conf.Title, days=delta.days, hours=0)
            dialog_result = dialog.ShowModal()
            days, hours = dialog.GetValues()
            if (wx.ID_OK == dialog_result) and (days or hours):
                db.move_future_dates(days, hours)
                wx.MessageBox(
                    "Set timestamp of %s %s%s back." % (
                        util.plural("message", future_count),
                        util.plural("day", days) if days else "",
                        (" and " if days else "") +
                        util.plural("hour", hours) if hours else "",
                    ),
                    conf.Title, wx.OK)



class DatabasePage(wx.Panel):
    """
    A wx.Notebook page for managing a single database file, has its own
    Notebook with a number of pages for searching, browsing chat history and
    database tables, information and contact import.
    """

    def __init__(self, parent_notebook, title, db, memoryfs, skype_handler):
        wx.Panel.__init__(self, parent=parent_notebook)
        self.parent_notebook = parent_notebook
        self.Label = title

        self.pageorder = {} # {page: notebook index, }
        self.ready_to_close = False
        self.db = db
        self.db.register_consumer(self)
        self.memoryfs = memoryfs
        self.skype_handler = skype_handler
        parent_notebook.InsertPage(1, self, title)
        busy = controls.BusyPanel(self, "Loading \"%s\"." % db.filename)

        self.chat = None  # Currently viewed chat
        self.chats = None # All chats in database
        self.chat_filter = { # Filter for currently shown chat history
            "daterange": None,      # Current date range
            "startdaterange": None, # Initial date range
            "text": "",             # Text in message content
            "participants": None    # Messages from [skype name, ]
        }
        self.stats_sort_field = "name"

        # Create search structures and threads
        self.Bind(EVT_WORKER, self.on_searchall_result)
        self.Bind(EVT_CONTACT_WORKER, self.on_search_contacts_result)
        self.workers_search = {} # {search ID: workers.SearchThread, }
        self.worker_search_contacts = \
            workers.ContactSearchThread(self.on_search_contacts_callback)
        self.search_data_contact = {"id": None} # Current contacts search data

        sizer = self.Sizer = wx.BoxSizer(wx.VERTICAL)

        sizer_header = wx.BoxSizer(wx.HORIZONTAL)
        label_title = self.label_title = wx.StaticText(parent=self, label="")
        sizer_header.Add(label_title, flag=wx.ALIGN_CENTER_VERTICAL)
        sizer_header.AddStretchSpacer()


        self.label_search = wx.StaticText(self, -1, "&Search in messages:")
        sizer_header.Add(self.label_search, border=5,
                         flag=wx.RIGHT | wx.ALIGN_CENTER_VERTICAL)
        edit_search = self.edit_searchall = controls.TextCtrlAutoComplete(
            self, description=conf.HistorySearchDescription,
            size=(300, -1), style=wx.TE_PROCESS_ENTER)
        # Restore last search text, if any
        if conf.SearchHistory and conf.SearchHistory[-1] != "":
            edit_search.Value = conf.SearchHistory[-1]
        else: # Clear the empty search flag
            conf.SearchHistory = conf.SearchHistory[:-1]
        edit_search.SetChoices(conf.SearchHistory)
        self.Bind(wx.EVT_TEXT_ENTER, self.on_searchall, edit_search)
        tb = self.tb_search = wx.ToolBar(parent=self,
                                         style=wx.TB_FLAT | wx.TB_NODIVIDER)

        bmp = wx.ArtProvider.GetBitmap(wx.ART_GO_FORWARD, wx.ART_TOOLBAR,
                                       (16, 16))
        tb.SetToolBitmapSize(bmp.Size)
        tb.AddLabelTool(wx.ID_FIND, "", bitmap=bmp, shortHelp="Start search")
        tb.Realize()
        self.Bind(wx.EVT_TOOL, self.on_searchall, id=wx.ID_FIND)
        sizer_header.Add(edit_search, border=5,
                     flag=wx.RIGHT | wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        sizer_header.Add(tb, flag=wx.ALIGN_RIGHT | wx.GROW)
        sizer.Add(sizer_header,
                  border=5, flag=wx.LEFT | wx.RIGHT | wx.TOP | wx.GROW)
        sizer.Layout() # To avoid searchbox moving around during page creation

        bookstyle = wx.lib.agw.fmresources.INB_LEFT
        if (wx.version().startswith("2.8") and sys.version.startswith("2.")
        and sys.version[:5] < "2.7.3"):
            # wx 2.8 + Python below 2.7.3: labelbook can partly cover tab area
            bookstyle |= wx.lib.agw.fmresources.INB_FIT_LABELTEXT
        notebook = self.notebook = wx.lib.agw.labelbook.FlatImageBook(
            parent=self, agwStyle=bookstyle, style=wx.BORDER_STATIC)

        il = wx.ImageList(32, 32)
        idx1 = il.Add(images.PageSearch.Bitmap)
        idx2 = il.Add(images.PageChats.Bitmap)
        idx3 = il.Add(images.PageInfo.Bitmap)
        idx4 = il.Add(images.PageTables.Bitmap)
        idx5 = il.Add(images.PageSQL.Bitmap)
        idx6 = il.Add(images.PageContacts.Bitmap)
        notebook.AssignImageList(il)

        self.create_page_search(notebook)
        self.create_page_chats(notebook)
        self.create_page_info(notebook)
        self.create_page_tables(notebook)
        self.create_page_sql(notebook)
        self.create_page_contacts(notebook)

        notebook.SetPageImage(0, idx1)
        notebook.SetPageImage(1, idx2)
        notebook.SetPageImage(2, idx3)
        notebook.SetPageImage(3, idx4)
        notebook.SetPageImage(4, idx5)
        notebook.SetPageImage(5, idx6)

        sizer.Add(notebook,proportion=1, border=5, flag=wx.GROW | wx.ALL)

        self.dialog_savefile = wx.FileDialog(
            parent=self,
            defaultDir=os.getcwd(),
            defaultFile="",
            style=wx.FD_OVERWRITE_PROMPT | wx.FD_SAVE | wx.RESIZE_BORDER
        )
        self.dialog_importfile = wx.FileDialog(
            parent=self,
            message="Select contacts file",
            defaultDir=os.getcwd(),
            wildcard="CSV spreadsheet (*.csv)|*.csv|All files (*.*)|*.*",
            style=wx.FD_FILE_MUST_EXIST | wx.FD_OPEN | wx.RESIZE_BORDER
        )

        self.TopLevelParent.page_db_latest = self
        self.TopLevelParent.console.run(
            "page = self.page_db_latest # Database tab")
        self.TopLevelParent.console.run("db = page.db # Skype database")

        self.Layout()
        self.toggle_filter(True)
        # Hack to get info-page multiline TextCtrls to layout without quirks.
        self.notebook.SetSelection(self.pageorder[self.page_info])
        # Hack to get chats-page filter split window to layout without quirks.
        self.notebook.SetSelection(self.pageorder[self.page_chats])
        self.notebook.SetSelection(self.pageorder[self.page_search])
        # Restore last active page
        if db.filename in conf.LastActivePage \
        and conf.LastActivePage[db.filename] != self.notebook.Selection:
            self.notebook.SetSelection(conf.LastActivePage[db.filename])

        try:
            self.load_data()
        finally:
            busy.Close()
        self.edit_searchall.SetFocus()
        wx.CallAfter(self.edit_searchall.SelectAll)
        if "linux2" == sys.platform and wx.version().startswith("2.8"):
            wx.CallAfter(self.split_panels)


    def create_page_chats(self, notebook):
        """Creates a page for listing and reading chats."""
        page = self.page_chats = wx.Panel(parent=notebook)
        self.pageorder[page] = len(self.pageorder)
        notebook.AddPage(page, "Chats")
        sizer = page.Sizer = wx.BoxSizer(wx.VERTICAL)
        splitter = self.splitter_chats = wx.SplitterWindow(
            parent=page, style=wx.BORDER_NONE
        )
        splitter.SetMinimumPaneSize(50)

        panel1 = self.panel_chats1 = wx.Panel(parent=splitter)
        sizer1 = panel1.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer_top = wx.BoxSizer(wx.HORIZONTAL)
        sizer_top.Add(
            wx.StaticText(panel1, label="A&ll chat entries in database:"),
            proportion=1, border=5, flag=wx.ALIGN_BOTTOM)
        list_chats = self.list_chats = controls.SortableListView(
            parent=panel1, style=wx.LC_REPORT)
        button_export_chats = self.button_export_chats = \
            wx.Button(parent=panel1, label="Export selected chats")
        button_export_allchats = self.button_export_allchats = \
            wx.Button(parent=panel1, label="Exp&ort all chats")
        sizer_top.Add(button_export_chats, flag=wx.ALIGN_CENTER_HORIZONTAL)
        sizer_top.Add((25, 0))
        sizer_top.Add(button_export_allchats, flag=wx.ALIGN_CENTER_HORIZONTAL)
        self.Bind(wx.EVT_BUTTON, self.on_export_chats, button_export_chats)
        self.Bind(wx.EVT_BUTTON, self.on_export_chats, button_export_allchats)
        sizer1.Add(sizer_top, border=5,
                   flag=wx.RIGHT | wx.LEFT | wx.BOTTOM | wx.GROW)
        self.Bind(wx.EVT_LIST_ITEM_ACTIVATED,
                  self.on_change_list_chats, list_chats)
        sizer1.Add(list_chats, proportion=1, border=5,
                   flag=wx.GROW | wx.LEFT | wx.RIGHT)

        panel2 = self.panel_chats2 = wx.Panel(parent=splitter)
        sizer2 = panel2.Sizer = wx.BoxSizer(wx.VERTICAL)

        splitter_stc = self.splitter_stc = \
            wx.SplitterWindow(parent=panel2, style=wx.BORDER_NONE)
        splitter_stc.SetMinimumPaneSize(50)
        panel_stc1 = self.panel_stc1 = wx.Panel(parent=splitter_stc)
        panel_stc2 = self.panel_stc2 = wx.Panel(parent=splitter_stc)
        sizer_stc1 = panel_stc1.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer_stc2 = panel_stc2.Sizer = wx.BoxSizer(wx.VERTICAL)

        sizer_header = wx.BoxSizer(wx.HORIZONTAL)
        label_chat = self.label_chat = wx.StaticText(
            parent=panel_stc1, label="&Chat:", name="chat_history_label")

        tb = self.tb_chat = \
            wx.ToolBar(parent=panel_stc1, style=wx.TB_FLAT | wx.TB_NODIVIDER)
        tb.SetToolBitmapSize((24, 24))
        tb.AddCheckTool(wx.ID_ZOOM_100,
                        bitmap=images.ToolbarMaximize.Bitmap,
                        shortHelp="Maximize chat panel  (Alt-M)")
        tb.AddCheckTool(wx.ID_PROPERTIES,
                        bitmap=images.ToolbarStats.Bitmap,
                        shortHelp="Toggle chat statistics  (Alt-I)")
        tb.AddCheckTool(wx.ID_MORE, bitmap=images.ToolbarFilter.Bitmap,
                        shortHelp="Toggle filter panel  (Alt-G)")
        tb.Realize()
        self.Bind(wx.EVT_TOOL, self.on_toggle_maximize, id=wx.ID_ZOOM_100)
        self.Bind(wx.EVT_TOOL, self.on_toggle_stats,    id=wx.ID_PROPERTIES)
        self.Bind(wx.EVT_TOOL, self.on_toggle_filter,   id=wx.ID_MORE)

        button_export = self.button_export_chat = \
            wx.Button(parent=panel_stc1, label="&Export messages to file")
        button_export.SetToolTipString(
            "Export currently shown messages to a file")
        self.Bind(wx.EVT_BUTTON, self.on_export_chat, button_export)
        sizer_header.Add(label_chat, proportion=1, border=5, flag=wx.LEFT |
                         wx.ALIGN_BOTTOM)
        sizer_header.Add(tb, flag=wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT)
        sizer_header.Add(button_export, border=15, flag=wx.LEFT |
                         wx.ALIGN_CENTER_VERTICAL)

        stc = self.stc_history = ChatContentSTC(
            parent=panel_stc1, style=wx.BORDER_STATIC, name="chat_history")
        stc.SetDatabasePage(self)
        html_stats = self.html_stats = wx.html.HtmlWindow(parent=panel_stc1)
        html_stats.Bind(wx.html.EVT_HTML_LINK_CLICKED,
                        self.on_click_html_stats)
        html_stats.Bind(wx.EVT_SCROLLWIN, self.on_scroll_html_stats)
        html_stats.Bind(wx.EVT_SIZE, self.on_size_html_stats)
        html_stats.Hide()

        sizer_stc1.Add(sizer_header, border=5, flag=wx.GROW | wx.RIGHT |
                       wx.BOTTOM)
        sizer_stc1.Add(stc, proportion=1, border=5, flag=wx.GROW)
        sizer_stc1.Add(html_stats, proportion=1, flag=wx.GROW)

        label_filter = \
            wx.StaticText(parent=panel_stc2, label="Find messages with &text:")
        edit_filter = self.edit_filtertext = wx.TextCtrl(
            parent=panel_stc2, size=(100, -1), style=wx.TE_PROCESS_ENTER)
        self.Bind(wx.EVT_TEXT_ENTER, self.on_filter_chat, edit_filter)
        edit_filter.SetToolTipString("Find messages containing the exact text")
        label_range = wx.StaticText(
            parent=panel_stc2, label="Show messages from time period:")
        range_date = self.range_date = \
            controls.RangeSlider(parent=panel_stc2, fmt="%Y-%m-%d")
        range_date.SetRange(None, None)
        label_list = \
            wx.StaticText(parent=panel_stc2, label="Sho&w messages from:")
        agw_style = (wx.LC_REPORT | wx.LC_NO_HEADER | wx.LC_SINGLE_SEL |
                     wx.lib.agw.ultimatelistctrl.ULC_NO_HIGHLIGHT |
                     wx.lib.agw.ultimatelistctrl.ULC_HRULES |
                     wx.lib.agw.ultimatelistctrl.ULC_SHOW_TOOLTIPS)
        if hasattr(wx.lib.agw.ultimatelistctrl, "ULC_USER_ROW_HEIGHT"):
            agw_style |= wx.lib.agw.ultimatelistctrl.ULC_USER_ROW_HEIGHT
        list_participants = self.list_participants = \
            wx.lib.agw.ultimatelistctrl.UltimateListCtrl(parent=panel_stc2,
                                                         agwStyle=agw_style)
        list_participants.InsertColumn(0, "")
        self.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select_participant,
                  list_participants)
        list_participants.EnableSelectionGradient()
        if hasattr(list_participants, "SetUserLineHeight"):
            list_participants.SetUserLineHeight(conf.AvatarImageSize[1] + 2)
        sizer_filter_buttons = wx.BoxSizer(wx.HORIZONTAL)
        button_filter_apply = self.button_chat_applyfilter = \
            wx.Button(parent=panel_stc2, label="A&pply filter")
        button_filter_export = self.button_chat_exportfilter = \
            wx.Button(parent=panel_stc2, label="Expo&rt filter")
        button_filter_reset = self.button_chat_unfilter = \
            wx.Button(parent=panel_stc2, label="Restore i&nitial")
        self.Bind(wx.EVT_BUTTON, self.on_filter_chat, button_filter_apply)
        self.Bind(wx.EVT_BUTTON, self.on_filterexport_chat,
                  button_filter_export)
        self.Bind(wx.EVT_BUTTON, self.on_filterreset_chat, button_filter_reset)
        button_filter_apply.SetToolTipString(
            "Filters the conversation by the specified text, "
            "date range and participants.")
        button_filter_export.SetToolTipString(
            "Exports filtered messages straight to file, "
            "without showing them (showing thousands of messages gets slow).")
        button_filter_reset.SetToolTipString(
            "Restores filter controls to initial values.")
        sizer_filter_buttons.Add(button_filter_apply)
        sizer_filter_buttons.AddSpacer(5)
        sizer_filter_buttons.Add(button_filter_export)
        sizer_filter_buttons.AddSpacer(5)
        sizer_filter_buttons.Add(button_filter_reset)
        sizer_filter_buttons.AddSpacer(5)
        sizer_stc2.Add(label_filter, border=5, flag=wx.LEFT)
        sizer_stc2.Add(edit_filter, border=5, flag=wx.GROW | wx.LEFT)
        sizer_stc2.AddSpacer(5)
        sizer_stc2.Add(label_range, border=5, flag=wx.LEFT)
        sizer_stc2.Add(range_date, border=5, flag=wx.GROW | wx.LEFT)
        sizer_stc2.AddSpacer(5)
        sizer_stc2.Add(label_list, border=5, flag=wx.LEFT)
        sizer_stc2.Add(list_participants, proportion=1, border=5,
                       flag=wx.GROW | wx.LEFT)
        sizer_stc2.AddSpacer(5)
        sizer_stc2.Add(sizer_filter_buttons, proportion=0, border=5,
                       flag=wx.GROW | wx.LEFT | wx.RIGHT)

        splitter_stc.SplitVertically(panel_stc1, panel_stc2, sashPosition=0)
        splitter_stc.Unsplit(panel_stc2) # Hide filter panel
        sizer2.Add(splitter_stc, proportion=1, border=5, flag=wx.GROW | wx.ALL)

        sizer.AddSpacer(10)
        sizer.Add(splitter, proportion=1, flag=wx.GROW)
        splitter.SplitHorizontally(panel1, panel2, sashPosition=self.Size[1]/3)
        panel2.Enabled = False


    def create_page_search(self, notebook):
        """Creates a page for searching chats."""
        page = self.page_search = wx.Panel(parent=notebook)
        self.pageorder[page] = len(self.pageorder)
        notebook.AddPage(page, "Search")
        sizer = page.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer_top = wx.BoxSizer(wx.HORIZONTAL)

        label_html = self.label_html = \
            wx.html.HtmlWindow(page, style=wx.html.HW_SCROLLBAR_NEVER)
        label_html.SetFonts(normal_face=self.Font.FaceName,
                            fixed_face=self.Font.FaceName, sizes=[8] * 7)
        label_html.SetPage(step.Template(templates.SEARCH_HELP_SHORT).expand())

        tb = self.tb_search_settings = \
            wx.ToolBar(parent=page, style=wx.TB_FLAT | wx.TB_NODIVIDER)
        tb.MinSize = (195, -1)
        tb.SetToolBitmapSize((24, 24))
        tb.AddRadioTool(wx.ID_INDEX, bitmap=images.ToolbarMessage.Bitmap,
            shortHelp="Search in message body")
        tb.AddRadioTool(wx.ID_PREVIEW, bitmap=images.ToolbarContact.Bitmap,
            shortHelp="Search in contact information")
        tb.AddRadioTool(wx.ID_ABOUT, bitmap=images.ToolbarTitle.Bitmap,
            shortHelp="Search in chat title and participants")
        tb.AddRadioTool(wx.ID_STATIC, bitmap=images.ToolbarTables.Bitmap,
            shortHelp="Search in all columns of all database tables")
        tb.AddSeparator()
        tb.AddCheckTool(wx.ID_NEW, bitmap=images.ToolbarTabs.Bitmap,
            shortHelp="New tab for each search  (Alt-N)", longHelp="")
        tb.AddSimpleTool(wx.ID_STOP, bitmap=images.ToolbarStopped.Bitmap,
            shortHelpString="Stop current search, if any")
        tb.Realize()
        tb.ToggleTool(wx.ID_INDEX, conf.SearchInMessageBody)
        tb.ToggleTool(wx.ID_ABOUT, conf.SearchInChatInfo)
        tb.ToggleTool(wx.ID_PREVIEW, conf.SearchInContacts)
        tb.ToggleTool(wx.ID_STATIC, conf.SearchInTables)
        tb.ToggleTool(wx.ID_NEW, conf.SearchInNewTab)
        for id in [wx.ID_INDEX, wx.ID_ABOUT, wx.ID_PREVIEW, wx.ID_STATIC,
                   wx.ID_NEW]:
            self.Bind(wx.EVT_TOOL, self.on_searchall_toggle_toolbar, id=id)
        self.Bind(wx.EVT_TOOL, self.on_searchall_stop, id=wx.ID_STOP)

        if conf.SearchInChatInfo:
            self.label_search.Label = "&Search in chat info:"
        elif conf.SearchInContacts:
            self.label_search.Label = "&Search in contacts:"
        elif conf.SearchInTables:
            self.label_search.Label = "&Search in all tables:"

        html = self.html_searchall = controls.TabbedHtmlWindow(parent=page)
        default = step.Template(templates.SEARCH_WELCOME_HTML).expand()
        html.SetDefaultPage(default)
        # Background colours in wx Linux behave strangely
        html.SetTabAreaColour(self.label_title.BackgroundColour)
        html.SetDeleteCallback(self.on_delete_tab_callback)
        label_html.Bind(wx.html.EVT_HTML_LINK_CLICKED,
                        self.on_click_searchall_result)
        html.Bind(wx.html.EVT_HTML_LINK_CLICKED,
                  self.on_click_searchall_result)
        html.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_change_searchall_tab)
        html.Font.PixelSize = (0, 8)

        label_html.BackgroundColour = tb.BackgroundColour
        
        sizer_top.Add(label_html, proportion=1, flag=wx.GROW)
        sizer_top.Add(tb, border=5, flag=wx.TOP | wx.RIGHT |
                      wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT)
        sizer.Add(sizer_top, border=5, flag=wx.TOP | wx.RIGHT | wx.GROW)
        sizer.Add(html, border=5, proportion=1,
                  flag=wx.GROW | wx.LEFT | wx.RIGHT | wx.BOTTOM)
        wx.CallAfter(label_html.Show)


    def create_page_tables(self, notebook):
        """Creates a page for listing and browsing tables."""
        page = self.page_tables = wx.Panel(parent=notebook)
        self.pageorder[page] = len(self.pageorder)
        notebook.AddPage(page, "Data tables")
        sizer = page.Sizer = wx.BoxSizer(wx.HORIZONTAL)
        splitter = self.splitter_tables = wx.SplitterWindow(
            parent=page, style=wx.BORDER_NONE
        )
        splitter.SetMinimumPaneSize(50)

        panel1 = wx.Panel(parent=splitter)
        sizer1 = panel1.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer1.Add(wx.StaticText(parent=panel1,
            label="&Tables:"), border=5, flag=wx.LEFT | wx.TOP | wx.BOTTOM)
        tree = self.tree_tables = wx.gizmos.TreeListCtrl(
            parent=panel1,
            style=wx.TR_DEFAULT_STYLE
            #| wx.TR_HAS_BUTTONS
            #| wx.TR_TWIST_BUTTONS
            #| wx.TR_ROW_LINES
            #| wx.TR_COLUMN_LINES
            #| wx.TR_NO_LINES
            | wx.TR_FULL_ROW_HIGHLIGHT
        )
        tree.AddColumn("Table")
        tree.AddColumn("Info")
        tree.AddRoot("Loading data..")
        tree.SetMainColumn(0)
        tree.SetColumnAlignment(1, wx.ALIGN_RIGHT)
        self.Bind(wx.EVT_TREE_ITEM_ACTIVATED, self.on_change_list_tables, tree)

        sizer1.Add(tree, proportion=1,
                   border=5, flag=wx.GROW | wx.LEFT | wx.TOP | wx.BOTTOM)

        panel2 = wx.Panel(parent=splitter)
        sizer2 = panel2.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer_tb = wx.BoxSizer(wx.HORIZONTAL)
        tb = self.tb_grid = wx.ToolBar(
            parent=panel2, style=wx.TB_FLAT | wx.TB_NODIVIDER)
        bmp_tb = images.ToolbarInsert.Bitmap
        tb.SetToolBitmapSize(bmp_tb.Size)
        tb.AddLabelTool(id=wx.ID_ADD, label="Insert new row.",
                        bitmap=bmp_tb, shortHelp="Add new row.")
        tb.AddLabelTool(id=wx.ID_DELETE, label="Delete current row.",
            bitmap=images.ToolbarDelete.Bitmap, shortHelp="Delete row.")
        tb.AddSeparator()
        tb.AddLabelTool(id=wx.ID_SAVE, label="Commit",
                        bitmap=images.ToolbarCommit.Bitmap,
                        shortHelp="Commit changes to database.")
        tb.AddLabelTool(id=wx.ID_UNDO, label="Rollback",
            bitmap=images.ToolbarRollback.Bitmap,
            shortHelp="Rollback changes and restore original values.")
        tb.EnableTool(wx.ID_ADD, False)
        tb.EnableTool(wx.ID_DELETE, False)
        tb.EnableTool(wx.ID_UNDO, False)
        tb.EnableTool(wx.ID_SAVE, False)
        self.Bind(wx.EVT_TOOL, handler=self.on_insert_row, id=wx.ID_ADD)
        self.Bind(wx.EVT_TOOL, handler=self.on_delete_row, id=wx.ID_DELETE)
        self.Bind(wx.EVT_TOOL, handler=self.on_commit_table, id=wx.ID_SAVE)
        self.Bind(wx.EVT_TOOL, handler=self.on_rollback_table, id=wx.ID_UNDO)
        tb.Realize() # should be called after adding tools
        label_table = self.label_table = wx.StaticText(parent=panel2, label="")
        button_reset = self.button_reset_grid_table = \
            wx.Button(parent=panel2, label="&Reset filter/sort")
        button_reset.SetToolTipString("Resets all applied sorting "
                                      "and filtering.")
        button_reset.Bind(wx.EVT_BUTTON, self.on_button_reset_grid)
        button_export = self.button_export_table = \
            wx.Button(parent=panel2, label="&Export to file")
        button_export.MinSize = (100, -1)
        button_export.SetToolTipString("Export rows to a file.")
        button_export.Bind(wx.EVT_BUTTON, self.on_button_export_grid)
        button_export.Enabled = False
        sizer_tb.Add(label_table, flag=wx.ALIGN_CENTER_VERTICAL)
        sizer_tb.AddStretchSpacer()
        sizer_tb.Add(button_reset, border=5, flag=wx.BOTTOM | wx.RIGHT |
                     wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        sizer_tb.Add(button_export, border=5, flag=wx.BOTTOM | wx.RIGHT |
                     wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        sizer_tb.Add(tb, flag=wx.ALIGN_RIGHT)
        grid = self.grid_table = wx.grid.Grid(parent=panel2)
        grid.SetToolTipString("Double click on column header to sort, "
                              "right click to filter.")
        grid.Bind(wx.grid.EVT_GRID_LABEL_LEFT_DCLICK, self.on_sort_grid_column)
        grid.GridWindow.Bind(wx.EVT_MOTION, self.on_mouse_over_grid)
        grid.Bind(wx.grid.EVT_GRID_LABEL_RIGHT_CLICK,
                  self.on_filter_grid_column)
        grid.Bind(wx.grid.EVT_GRID_CELL_CHANGE, self.on_change_table)
        label_help = wx.StaticText(panel2, wx.NewId(),
            "Double-click on column header to sort, right click to filter.")
        label_help.ForegroundColour = "grey"
        sizer2.Add(sizer_tb, border=5, flag=wx.GROW | wx.LEFT | wx.TOP)
        sizer2.Add(grid, border=5, proportion=2,
                   flag=wx.GROW | wx.LEFT | wx.RIGHT)
        sizer2.Add(label_help, border=5, flag=wx.LEFT | wx.TOP)

        sizer.Add(splitter, proportion=1, flag=wx.GROW)
        splitter.SplitVertically(panel1, panel2, 270)


    def create_page_sql(self, notebook):
        """Creates a page for executing arbitrary SQL."""
        page = self.page_sql = wx.Panel(parent=notebook)
        self.pageorder[page] = len(self.pageorder)
        notebook.AddPage(page, "SQL window")
        sizer = page.Sizer = wx.BoxSizer(wx.VERTICAL)
        splitter = self.splitter_sql = \
            wx.SplitterWindow(parent=page, style=wx.BORDER_NONE)
        splitter.SetMinimumPaneSize(50)

        panel1 = self.panel_sql1 = wx.Panel(parent=splitter)
        sizer1 = panel1.Sizer = wx.BoxSizer(wx.VERTICAL)
        label_stc = wx.StaticText(parent=panel1, label="SQ&L:")
        stc = self.stc_sql = controls.SQLiteTextCtrl(parent=panel1,
            style=wx.BORDER_STATIC | wx.TE_PROCESS_TAB | wx.TE_PROCESS_ENTER)
        stc.Bind(wx.EVT_KEY_DOWN, self.on_keydown_sql)
        stc.SetText(conf.SQLWindowTexts.get(self.db.filename, ""))
        sizer1.Add(label_stc, border=5, flag=wx.ALL)
        sizer1.Add(stc, border=5, proportion=1, flag=wx.GROW | wx.LEFT)

        panel2 = self.panel_sql2 = wx.Panel(parent=splitter)
        sizer2 = panel2.Sizer = wx.BoxSizer(wx.VERTICAL)
        label_help = wx.StaticText(panel2, label=
            "Ctrl-Space shows autocompletion list. Alt-Enter runs the query "
            "contained in currently selected text or on the current line.")
        label_help.ForegroundColour = "grey"
        sizer_buttons = wx.BoxSizer(wx.HORIZONTAL)
        button_sql = self.button_sql = wx.Button(panel2, label="Execute S&QL")
        self.Bind(wx.EVT_BUTTON, self.on_button_sql, button_sql)
        button_reset = self.button_reset_grid_sql = \
            wx.Button(parent=panel2, label="&Reset filter/sort")
        button_reset.SetToolTipString("Resets all applied sorting "
                                      "and filtering.")
        button_reset.Bind(wx.EVT_BUTTON, self.on_button_reset_grid)
        button_export = self.button_export_sql = \
            wx.Button(parent=panel2, label="&Export to file")
        button_export.SetToolTipString("Export result to a file.")
        button_export.Bind(wx.EVT_BUTTON, self.on_button_export_grid)
        button_export.Enabled = False
        sizer_buttons.Add(button_sql, flag=wx.ALIGN_LEFT)
        sizer_buttons.AddStretchSpacer()
        sizer_buttons.Add(button_reset, border=5,
                          flag=wx.ALIGN_RIGHT | wx.RIGHT)
        sizer_buttons.Add(button_export, flag=wx.ALIGN_RIGHT)
        grid = self.grid_sql = wx.grid.Grid(parent=panel2)
        grid.Bind(wx.grid.EVT_GRID_LABEL_LEFT_DCLICK,
                  self.on_sort_grid_column)
        grid.Bind(wx.grid.EVT_GRID_LABEL_RIGHT_CLICK,
                  self.on_filter_grid_column)
        grid.Bind(wx.EVT_SCROLLWIN, self.on_scroll_grid_sql)
        grid.Bind(wx.EVT_SCROLL_THUMBRELEASE, self.on_scroll_grid_sql)
        grid.Bind(wx.EVT_SCROLL_CHANGED, self.on_scroll_grid_sql)
        grid.Bind(wx.EVT_KEY_DOWN, self.on_scroll_grid_sql)
        grid.GridWindow.Bind(wx.EVT_MOTION, self.on_mouse_over_grid)
        label_help_grid = wx.StaticText(panel2, wx.NewId(),
            "Double-click on column header to sort, right click to filter.")
        label_help_grid.ForegroundColour = "grey"

        sizer2.Add(label_help, border=5, flag=wx.GROW | wx.LEFT | wx.BOTTOM)
        sizer2.Add(sizer_buttons, border=5, flag=wx.GROW | wx.ALL)
        sizer2.Add(grid, border=5, proportion=2,
                   flag=wx.GROW | wx.LEFT | wx.RIGHT)
        sizer2.Add(label_help_grid, border=5, flag=wx.GROW | wx.LEFT | wx.TOP)

        sizer.Add(splitter, proportion=1, flag=wx.GROW)
        sash_pos = self.Size[1] / 3
        splitter.SplitHorizontally(panel1, panel2, sashPosition=self.Size[1]/3)


    def create_page_contacts(self, notebook):
        """Creates a page for importing contacts from file."""
        page = self.page_contacts = wx.Panel(parent=notebook)
        self.pageorder[page] = len(self.pageorder)
        notebook.AddPage(page, "Contacts+")
        sizer = page.Sizer = wx.BoxSizer(wx.VERTICAL)
        splitter = self.splitter_import = wx.SplitterWindow(
            parent=page, style=wx.BORDER_NONE
        )
        splitter.SetMinimumPaneSize(50)

        panel1 = wx.Panel(parent=splitter)
        sizer1 = panel1.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer_header = wx.BoxSizer(wx.HORIZONTAL)

        label_header = wx.StaticText(parent=panel1, 
            label="Import people to your Skype contacts from a CSV file, "
                  "like ones exported from MSN or GMail.\n"
                  "Skype needs to be running and logged in.\n\n"
                  "For exporting your MSN contacts, log in to hotmail.com "
                  "with your MSN account and find \"Export contacts\" under "
                  "Options.\n"
                  "For exporting your GMail contacts, log in to gmail.com and "
                  "find \"Export...\" under \"Contacts\" -> \"More\".")
        label_header.ForegroundColour = "grey"
        button_import = self.button_import_file = \
            wx.Button(panel1, label="Se&lect contacts file")
        button_import.Bind(wx.EVT_BUTTON, self.on_choose_import_file)
        sizer_header.Add(button_import, border=10,
                         flag=wx.RIGHT | wx.ALIGN_CENTER_VERTICAL)
        sizer_header.Add(label_header, border=60, flag=wx.LEFT)
        label_source = self.label_import_source = \
            wx.StaticText(parent=panel1, label="C&ontacts in source file:")
        sourcelist = self.list_import_source = \
            controls.SortableListView(parent=panel1, style=wx.LC_REPORT)
        source_columns = ["Name", "E-mail", "Phone"]
        sourcelist.SetColumnCount(len(source_columns))
        for col, name in enumerate(source_columns):
            sourcelist.InsertColumn(col + 1, name)
        sourcelist.Bind(wx.EVT_LIST_ITEM_SELECTED,
                        self.on_select_import_sourcelist)
        sourcelist.Bind(wx.EVT_LIST_ITEM_DESELECTED,
                        self.on_select_import_sourcelist)
        sourcelist.Bind(wx.EVT_LIST_ITEM_ACTIVATED,
                        self.on_import_search)

        sizer1.Add(sizer_header, border=5, flag=wx.ALL)
        sizer1.Add(label_source, border=5, flag=wx.ALL)
        sizer1.Add(sourcelist, border=5, proportion=1,
                   flag=wx.GROW | wx.LEFT | wx.RIGHT)

        panel2 = wx.Panel(parent=splitter)
        sizer2 = panel2.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer_buttons = wx.BoxSizer(wx.HORIZONTAL)
        button_search_selected = self.button_import_search_selected = \
            wx.Button(panel2, label="Search for selected contacts in Skype")
        button_select_all = self.button_import_select_all = \
            wx.Button(panel2, label="Select all")
        self.Bind(wx.EVT_BUTTON, self.on_import_search, button_search_selected)
        self.Bind(wx.EVT_BUTTON, self.on_import_select_all, button_select_all)
        button_search_selected.SetToolTipString("Search for the selected "
            "contacts through the running Skype application.")
        button_search_selected.Enabled = button_select_all.Enabled = False
        label_search = wx.StaticText(parent=panel2,
                                     label="Skype use&rbase search:")
        edit_search = self.edit_import_search_free = wx.TextCtrl(
            parent=panel2, size=(100, -1), style=wx.TE_PROCESS_ENTER)
        button_search_free = self.button_import_search_free = \
            wx.Button(panel2, label="Search in Skype")
        self.Bind(wx.EVT_TEXT_ENTER, self.on_import_search, edit_search)
        self.Bind(wx.EVT_BUTTON, self.on_import_search, button_search_free)
        for control in [label_search, edit_search, button_search_free]:
            control.SetToolTipString("Search for the entered value in "
                                     "Skype userbase.")

        sizer_buttons.Add(button_search_selected, flag=wx.ALIGN_LEFT)
        sizer_buttons.Add(button_select_all, border=5, flag=wx.LEFT)
        sizer_buttons.AddStretchSpacer()
        sizer_buttons.Add(label_search, flag=wx.ALIGN_CENTER_VERTICAL)
        sizer_buttons.Add(edit_search, border=5, flag=wx.LEFT)
        sizer_buttons.Add(button_search_free, border=5, flag=wx.LEFT)

        label_searchinfo = wx.StaticText(parent=panel2,
            label="Skype will be launched if not already running. Might bring "
                  "up a notification screen in Skype to allow access for "
                  "%s.\nSearching for many contacts at once can "
                  "take a long time." % conf.Title)
        label_searchinfo.ForegroundColour = "grey"

        sizer_resultlabel = wx.BoxSizer(wx.HORIZONTAL)
        label_result = self.label_import_result = \
            wx.StaticText(parent=panel2, label="Contacts found in Sk&ype:")
        resultlist = self.list_import_result = \
            controls.SortableListView(parent=panel2, style=wx.LC_REPORT)
        result_columns = ["#", "Name", "Skype handle", "Already added",
            "Phone", "City", "Country", "Gender", "Birthday", "Language"]
        resultlist.SetColumnCount(len(result_columns))
        for col, name in enumerate(result_columns):
            resultlist.InsertColumn(col + 1, name)

        resultlist.Bind(
            wx.EVT_LIST_ITEM_SELECTED,   self.on_select_import_resultlist)
        resultlist.Bind(
            wx.EVT_LIST_ITEM_DESELECTED, self.on_select_import_resultlist)
        resultlist.Bind(wx.EVT_LIST_ITEM_ACTIVATED,self.on_import_add_contacts)

        sizer_footer = wx.BoxSizer(wx.HORIZONTAL)
        button_add = self.button_import_add = \
            wx.Button(panel2, label="Add the selected to your Skype contacts")
        button_clear = self.button_import_clear = \
            wx.Button(panel2, label="Clear selected from list")
        self.Bind(wx.EVT_BUTTON, self.on_import_add_contacts, button_add)
        self.Bind(wx.EVT_BUTTON, self.on_import_clear_contacts, button_clear)
        button_add.SetToolTipString("Opens an authorization request in Skype")
        button_add.Enabled = button_clear.Enabled = False
        sizer_footer.Add(button_add, flag=wx.ALIGN_LEFT)
        sizer_footer.AddStretchSpacer()
        sizer_footer.Add(button_clear, flag=wx.ALIGN_RIGHT)

        sizer2.Add(sizer_buttons, border=5, flag=wx.GROW | wx.ALL)
        sizer_resultlabel.Add(label_result, flag=wx.ALIGN_BOTTOM)
        sizer_resultlabel.Add(label_searchinfo, border=60, flag=wx.LEFT)
        sizer2.Add(sizer_resultlabel, border=5, flag=wx.ALL)
        sizer2.Add(resultlist, border=5, proportion=1,
                   flag=wx.GROW | wx.LEFT | wx.RIGHT)
        sizer2.Add(sizer_footer, border=5, flag=wx.GROW | wx.ALL)

        sizer.Add(splitter, proportion=1, flag=wx.GROW)
        splitter.SplitHorizontally(panel1, panel2, sashPosition=self.Size[1]/3)


    def create_page_info(self, notebook):
        """Creates a page for seeing general database information."""
        page = self.page_info = wx.Panel(parent=notebook)
        self.pageorder[page] = len(self.pageorder)
        notebook.AddPage(page, "Information")
        sizer = page.Sizer = wx.BoxSizer(wx.HORIZONTAL)

        panel1 = wx.Panel(parent=page)
        panel2 = wx.Panel(parent=page)
        panel1.BackgroundColour = panel2.BackgroundColour = wx.WHITE
        sizer1 = panel1.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer_account = wx.BoxSizer(wx.HORIZONTAL)
        label_account = wx.StaticText(parent=panel1,
                                      label="Main account information")
        label_account.Font = wx.Font(10, wx.FONTFAMILY_SWISS,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD, face=self.Font.FaceName)
        sizer1.Add(label_account, border=5, flag=wx.ALL)

        account = self.db.account or {}
        bmp_panel = wx.Panel(parent=panel1)
        bmp_panel.Sizer = wx.BoxSizer(wx.VERTICAL)
        bmp = skypedata.get_avatar(account) or images.AvatarDefaultLarge.Bitmap
        bmp_static = wx.StaticBitmap(bmp_panel, bitmap=bmp)
        sizer_accountinfo = wx.FlexGridSizer(cols=2, vgap=3, hgap=10)
        fields = ["fullname", "skypename", "mood_text", "phone_mobile",
                  "phone_home", "phone_office", "emails", "country",
                  "province", "city", "homepage", "gender", "birthday",
                  "languages", "nrof_authed_buddies", "about",
                  "skypeout_balance", ]

        for field in fields:
            if field in account and self.db.account[field]:
                value = account[field]
                if "emails" == field:
                    value = ", ".join(value.split(" "))
                elif "gender" == field:
                    value = {1: "male", 2: "female"}.get(value, "")
                elif "birthday" == field:
                    try:
                        value = str(value)
                        value = "-".join([value[:4], value[4:6], value[6:]])
                    except Exception, e:
                        pass
                if value:
                    if "skypeout_balance" == field:
                        value = value / (
                                10.0 ** account.get("skypeout_precision", 2))
                        value = "%s %s" % (value,
                                account.get("skypeout_balance_currency", ""))
                    if not isinstance(value, basestring):
                        value = str(value) 
                    title = skypedata.ACCOUNT_FIELD_TITLES.get(field, field)
                    lbltext = wx.StaticText(parent=panel1, label="%s:" % title)
                    valtext = wx.TextCtrl(parent=panel1, value=value,
                        style=wx.NO_BORDER | wx.TE_MULTILINE | wx.TE_RICH)
                    valtext.BackgroundColour = panel1.BackgroundColour
                    valtext.SetEditable(False)
                    lbltext.ForegroundColour = wx.Colour(102, 102, 102)
                    sizer_accountinfo.Add(lbltext, border=5, flag=wx.LEFT)
                    sizer_accountinfo.Add(valtext, proportion=1, flag=wx.GROW)

        sizer_accountinfo.AddGrowableCol(1, 1)
        bmp_panel.Sizer.Add(bmp_static, border=2, flag=wx.GROW | wx.TOP)
        sizer_account.Add(bmp_panel, border=10, flag=wx.LEFT | wx.RIGHT)
        sizer_account.Add(sizer_accountinfo, proportion=1, flag=wx.GROW)
        sizer1.Add(sizer_account, border=20, proportion=1,
                   flag=wx.TOP | wx.GROW)

        sizer2 = panel2.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer_file = wx.FlexGridSizer(cols=2, vgap=3, hgap=10)
        label_file = wx.StaticText(parent=panel2,
                                   label="Database file information")
        label_file.Font = wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL,
                                  wx.FONTWEIGHT_BOLD, face=self.Font.FaceName)
        sizer2.Add(label_file, border=5, flag=wx.ALL)

        names = ["edit_info_path", "edit_info_size", "edit_info_modified",
                 "edit_info_sha1", "edit_info_md5"]
        labels = ["Full path", "File size", "Last modified",
                  "SHA-1 checksum", "MD5 checksum"]
        for i, (name, label) in enumerate(zip(names, labels)):
            labeltext = wx.StaticText(parent=panel2, label="%s:" % label)
            labeltext.ForegroundColour = wx.Colour(102, 102, 102)
            valuetext = wx.TextCtrl(parent=panel2, value="Analyzing..",
                style=wx.NO_BORDER | wx.TE_MULTILINE | wx.TE_RICH)
            valuetext.BackgroundColour = panel2.BackgroundColour
            valuetext.SetEditable(False)
            sizer_file.Add(labeltext, border=5, flag=wx.LEFT)
            sizer_file.Add(valuetext, proportion=1, flag=wx.GROW)
            setattr(self, name, valuetext)
        self.edit_info_path.Value = self.db.filename
        sizer_file.AddSpacer(10)
        button_refresh = self.button_refresh_fileinfo = \
            wx.Button(parent=panel2, label="Refresh")
        button_refresh.Enabled = False
        sizer_file.Add(button_refresh)
        self.Bind(wx.EVT_BUTTON, lambda e: self.update_file_info(),
                  button_refresh)

        sizer_file.AddGrowableCol(1, 1)
        sizer2.Add(sizer_file, border=20, proportion=1, flag=wx.TOP | wx.GROW)

        sizer.Add(panel1, proportion=1, border=5,
                  flag=wx.LEFT  | wx.TOP | wx.BOTTOM | wx.GROW)
        sizer.Add(panel2, proportion=1, border=5,
                  flag=wx.RIGHT | wx.TOP | wx.BOTTOM | wx.GROW)


    def split_panels(self):
        """
        Splits all SplitterWindow panels. To be called after layout in
        Linux wx 2.8, as otherwise panels do not get sized properly.
        """
        if not self:
            return
        sash_pos = self.Size[1] / 3
        panel1, panel2 = self.splitter_chats.Children
        self.splitter_chats.Unsplit()
        self.splitter_chats.SplitHorizontally(panel1, panel2, sash_pos)
        panel1, panel2 = self.splitter_tables.Children
        self.splitter_tables.Unsplit()
        self.splitter_tables.SplitVertically(panel1, panel2, 270)
        panel1, panel2 = self.splitter_sql.Children
        self.splitter_sql.Unsplit()
        self.splitter_sql.SplitHorizontally(panel1, panel2, sash_pos)
        panel1, panel2 = self.splitter_import.Children
        self.splitter_import.Unsplit()
        self.splitter_import.SplitHorizontally(panel1, panel2, sash_pos)
        wx.CallLater(1000, lambda: self and 
                     (self.tree_tables.SetColumnWidth(0, -1),
                      self.tree_tables.SetColumnWidth(1, -1)))


    def update_file_info(self):
        """Updates the file page with current data."""
        self.db.update_fileinfo()
        self.edit_info_size.Value = "%s (%s bytes)" % \
            (util.format_bytes(self.db.filesize), self.db.filesize)
        self.edit_info_modified.Value = \
            self.db.last_modified.strftime("%Y-%m-%d %H:%M:%S")
        BLOCKSIZE = 1048576
        sha1, md5 = hashlib.sha1(), hashlib.md5()
        try:
            with open(self.db.filename, "rb") as f:
                buf = f.read(BLOCKSIZE)
                while len(buf):
                    sha1.update(buf), md5.update(buf)
                    buf = f.read(BLOCKSIZE)
            self.edit_info_sha1.Value = sha1.hexdigest()
            self.edit_info_md5.Value = md5.hexdigest()
        except Exception, e:
            self.edit_info_sha1.Value = self.edit_info_md5.Value = u"%s" % e
        self.button_refresh_fileinfo.Enabled = True


    def on_choose_import_file(self, event):
        """Handler for clicking to choose a CSV file for contact import."""
        contacts = None
        if wx.ID_OK == self.dialog_importfile.ShowModal():
            filename = self.dialog_importfile.GetPath()
            try:
                contacts = skypedata.import_contacts_file(filename)
            except Exception, e:
                wx.MessageBox(
                    "Error reading \"%s\".\n\n%s" % (filename, e),
                    conf.Title, wx.OK | wx.ICON_WARNING
                )
        if contacts is not None:
            while self.list_import_source.ItemCount:
                self.list_import_source.DeleteItem(0)
            while self.list_import_result.ItemCount:
                self.list_import_result.DeleteItem(0)
            self.button_import_add.Enabled = False
            self.button_import_clear.Enabled = False
            self.button_import_search_selected.Enabled = False
            for i, contact in enumerate(contacts):
                cols = ["name", "e-mail", "phone"]
                self.list_import_source.PopulateRow(i, cols, contact)
            self.label_import_source.Label = \
                "C&ontacts in source file %s [%s]:" % (filename, len(contacts))
            self.label_import_result.Label = "Contacts found in Sk&ype:"
            self.button_import_select_all.Enabled = len(contacts)
            if self.list_import_source.ItemCount:
                for i in range(self.list_import_source.ColumnCount):
                    self.list_import_source.SetColumnWidth(i, wx.LIST_AUTOSIZE)
            main.logstatus_flash("Found %s in file %s.",
                                 util.plural("contact", contacts), filename)


    def on_select_import_sourcelist(self, event):
        """
        Handler when a row is selected in the import contacts source list,
        enables UI buttons.
        """
        count = self.list_import_source.GetSelectedItemCount()
        self.button_import_search_selected.Enabled = (count > 0)


    def on_select_import_resultlist(self, event):
        """
        Handler when a row is selected in the import contacts result list,
        enables UI buttons.
        """
        count = self.list_import_result.GetSelectedItemCount()
        self.button_import_add.Enabled = (count > 0)
        self.button_import_clear.Enabled = (count > 0)


    def on_import_select_all(self, event):
        """Handler for clicking to select all imported contacts."""
        map(self.list_import_source.Select,
            range(self.list_import_source.ItemCount))
        self.list_import_source.SetFocus()


    def on_import_search(self, event):
        """
        Handler for choosing to search Skype for contacts in the import source
        list.
        """
        if not self.skype_handler:
            msg = "Skype4Py not installed, cannot search."
            return wx.MessageBox(msg, conf.Title, wx.OK | wx.ICON_WARNING)
        search_values = []
        lst, lst2 = self.list_import_source, self.list_import_result
        if event.EventObject in [self.button_import_search_free,
                                 self.edit_import_search_free]:
            value = self.edit_import_search_free.Value.strip()
            if value:
                search_values.append(value)
                infotext = "Searching Skype userbase for \"%s\"." % value
        else:
            values_unique, contacts_unique = set(), set()
            selected = lst.GetFirstSelected()
            while selected >= 0:
                contact = lst.GetItemMappedData(selected)
                for key in ["name", "phone", "e-mail"]:
                    if contact[key] and contact[key] not in values_unique:
                        search_values.append(contact[key])
                        values_unique.add(contact[key])
                        contacts_unique.add(id(contact))
                selected = lst.GetNextSelected(selected)
            info = "\"%s\"" % "\", \"".join(search_values)
            if len(info) > 60:
                info = info[:60] + ".."
            infotext = "Searching Skype userbase for %s (%s)." \
                        % (util.plural("contact", contacts_unique), info)
        if search_values:
            main.logstatus_flash(infotext)
            while lst2.ItemCount:
                lst2.DeleteItem(0)
            self.button_import_add.Enabled = False
            self.button_import_clear.Enabled = False

            found = {} # { Skype handle: user data, }
            data = {"id": wx.NewId(), "handler": self.skype_handler,
                    "values": search_values}
            self.search_data_contact.update(data)
            self.worker_search_contacts.work(data)
            self.label_import_result.Label = "Contacts found in Sk&ype:"


    def on_search_contacts_result(self, event):
        """
        Handler for getting results from contact search thread, adds the
        results to the import list.
        """
        result = event.result
        # If search ID is different, results are from the previous search still
        lst2 = self.list_import_result
        if result["search"]["id"] == self.search_data_contact["id"]:
            lst2.Freeze()
            scrollpos = lst2.GetScrollPos(wx.VERTICAL)

            for user in result["results"]:
                index = lst2.ItemCount + 1
                cols, data = ["#"], {"user": user, "#": index}
                for k in ["FullName", "Handle", "IsAuthorized", "PhoneMobile",
                          "City", "Country", "Sex", "Birthday", "Language"]:
                    val = getattr(user, k)
                    if "IsAuthorized" == k:
                        val = "Yes" if val else "No"
                    elif "Sex" == k:
                        val = "" if ("UNKNOWN" == val) else val.lower()
                    cols.append(k)
                    data[k] = val
                index = lst2.ItemCount
                lst2.PopulateRow(index, cols, data)
                if user.IsAuthorized:
                    lst2.SetItemTextColour(index, "gray")
            if lst2.ItemCount:
                for i in range(lst2.ColumnCount):
                    lst2.SetColumnWidth(i, wx.LIST_AUTOSIZE)
                    if lst2.GetColumnWidth(i) < 60:
                        lst2.SetColumnWidth(i, wx.LIST_AUTOSIZE_USEHEADER)

            lst2.SetScrollPos(wx.VERTICAL, scrollpos)
            lst2.Thaw()
            self.label_import_result.Label = \
                "Contacts found in Sk&ype [%s]:" % lst2.ItemCount
            if "done" in result:
                main.logstatus_flash("Found %s in Skype userbase.",
                                     util.plural("contact", lst2.ItemCount))
            lst2.Update()


    def on_import_add_contacts(self, event):
        """
        Handler for adding an imported contact in Skype, opens an authorization
        request window in Skype.
        """
        lst = self.list_import_result
        selected, contacts = lst.GetFirstSelected(), []
        while selected >= 0:
            contacts.append(lst.GetItemMappedData(selected))
            selected = lst.GetNextSelected(selected)
        info = ", ".join([c["Handle"] for c in contacts])
        if len(info) > 60:
            info = info[:60] + ".."
        msg = "Add %s to your Skype contacts (%s)?" % (
              util.plural("person", contacts), info)
        if self.skype_handler and wx.OK == wx.MessageBox(msg,
            conf.Title, wx.OK | wx.CANCEL | wx.ICON_QUESTION
        ):
            busy = controls.BusyPanel(self,
                "Adding %s to your Skype contacts."
                % util.plural("person", contacts)
            )
            try:
                self.skype_handler.add_to_contacts(c["user"] for c in contacts)
                main.logstatus_flash("Added %s to your Skype contacts (%s).",
                                     util.plural("person", contacts), info)
            finally:
                busy.Close()


    def on_import_clear_contacts(self, event):
        """
        Handler for clicking to remove selected items from contact import
        result list.
        """
        selected,selecteds = self.list_import_result.GetFirstSelected(), []
        while selected >= 0:
            selecteds.append(selected)
            selected = self.list_import_result.GetNextSelected(selected)
        for i in range(len(selecteds)):
            # - i, as item count is getting smaller one by one
            selected = selecteds[i] - i
            data = self.list_import_result.GetItemMappedData(selected)
            self.list_import_result.DeleteItem(selected)
        self.label_import_result.Label = "Contacts found in Sk&ype [%s]:" \
                                         % self.list_import_result.ItemCount
        self.button_import_add.Enabled = False
        self.button_import_clear.Enabled = False


    def on_export_chat(self, event):
        """
        Handler for clicking to export a chat, displays a save file dialog and
        saves the current messages to file.
        """
        default = "Skype %s" % self.chat["title_long_lc"]
        self.dialog_savefile.Filename = util.safe_filename(default)
        self.dialog_savefile.Message = "Save chat"
        self.dialog_savefile.Wildcard = \
            "HTML document (*.html)|*.html|" \
            "Text document (*.txt)|*.txt|" \
            "CSV spreadsheet (*.csv)|*.csv"
        if wx.ID_OK == self.dialog_savefile.ShowModal():
            filename = self.dialog_savefile.GetPath()
            extname = ["html", "txt", "csv"][self.dialog_savefile.FilterIndex]
            if not filename.lower().endswith(".%s" % extname):
                filename += ".%s" % extname
            busy = controls.BusyPanel(
                self, "Exporting \"%s\"." % self.chat["title"]
            )
            main.logstatus("Exporting to %s.", filename)
            try:
                messages = self.stc_history.GetMessages()
                if export.export_chat(self.chat, messages, filename, self.db):
                    main.logstatus_flash("Exported %s.", filename)
                    util.start_file(filename)
                else:
                    main.logstatus_flash("Error exporting to %s.", filename)
                    wx.MessageBox("Error exporting to \"%s\"." % filename,
                                  conf.Title, wx.OK | wx.ICON_WARNING)
            finally:
                busy.Close()


    def on_export_chats(self, event):
        """
        Handler for clicking to export selected or all chats, displays a select
        folder dialog and exports chats to individual files under the folder.
        """
        do_all = (event.EventObject == self.button_export_allchats)
        chats = self.chats
        if not do_all:
            selected, chats = self.list_chats.GetFirstSelected(), []
            while selected >= 0:
                chats.append(self.list_chats.GetItemMappedData(selected))
                selected = self.list_chats.GetNextSelected(selected)
        if chats:
            self.dialog_savefile.Filename = "Filename will be ignored"
            self.dialog_savefile.Message = "Choose folder where to save chats"
            self.dialog_savefile.Wildcard = \
                "HTML document (*.html)|*.html|" \
                "Text document (*.txt)|*.txt|" \
                "CSV spreadsheet (*.csv)|*.csv"
        if chats and wx.ID_OK == self.dialog_savefile.ShowModal():
            filenames = []
            dirname = os.path.dirname(self.dialog_savefile.GetPath())
            extname = ["html", "txt", "csv"][self.dialog_savefile.FilterIndex]
            busy = controls.BusyPanel(
                self, "Exporting %s from \"%s\"\nas %s under %s." % 
                (util.plural("chat", chats), self.db.filename,
                extname.upper(), dirname))
            main.logstatus("Exporting %s from %s as %s under %s.",
                           util.plural("chat", chats),
                           self.db.filename, extname.upper(), dirname)
            errormsg = False
            try:
                for chat in chats:
                    main.status("Exporting %s", chat["title_long_lc"])
                    wx.GetApp().Yield()
                    if chat["message_count"] or not do_all:
                        f = "Skype %s.%s" % (chat["title_long_lc"], extname)
                        f = os.path.join(db_dirname, util.safe_filename(f))
                        f = util.unique_path(f)
                        messages = self.db.get_messages(chat)
                        if export.export_chat(chat, messages, f, self.db):
                            filenames.append(f)
                        else:
                            errormsg = 'An error occurred when saving "%s".' % f
                            break # break for chat in chats
                    else:
                        main.log("Skipping %s: no messages.",
                                 chat["title_long_lc"])
            except Exception, e:
                errormsg = "An unexpected error occurred when saving " \
                           "%s from \"%s\" as %s under %s: %s" % \
                           (util.plural("chat", chats),
                            extname.upper(), dirname, e)
            busy.Close()
            if not errormsg:
                main.logstatus_flash("Exported %s from %s as %s under %s.",
                                     util.plural("chat", filenames),
                                     self.db.filename, extname.upper(), dirname)
                util.start_file(dirname if len(filenames) > 1 else filenames[0])
            else:
                main.logstatus_flash(
                    "Failed to export %s from %s as %s under %s.",
                    util.plural("chat", chats), self.db.filename,
                    extname.upper(), dirname)
                wx.MessageBox(errormsg, conf.Title, wx.OK | wx.ICON_WARNING)


    def on_filterexport_chat(self, event):
        """
        Handler for clicking to export a chat filtering straight to file,
        displays a save file dialog and saves all filtered messages to file.
        """
        default = "Skype %s" % self.chat["title_long_lc"]
        self.dialog_savefile.Filename = util.safe_filename(default)
        self.dialog_savefile.Message = "Save chat"
        self.dialog_savefile.Wildcard = \
            "HTML document (*.html)|*.html|" \
            "Text document (*.txt)|*.txt|" \
            "CSV spreadsheet (*.csv)|*.csv"
        if wx.ID_OK == self.dialog_savefile.ShowModal():
            filename = self.dialog_savefile.GetPath()
            extname = ["html", "txt", "csv"][self.dialog_savefile.FilterIndex]
            if not filename.lower().endswith(".%s" % extname):
                filename += ".%s" % extname

            busy = controls.BusyPanel(self,
                   "Filtering and exporting \"%s\"." % self.chat["title"])
            try:
                filter_new = self.build_filter()
                filter_backup = self.stc_history.GetFilter()
                self.stc_history.SetFilter(filter_new)
                self.stc_history.RetrieveMessagesIfNeeded()
                messages_all = self.stc_history.GetRetrievedMessages()
                msgs = [m for m in messages_all
                        if not self.stc_history.IsMessageFilteredOut(m)]
                self.stc_history.SetFilter(filter_backup)
                if msgs:
                    main.logstatus("Filtering and exporting to %s.", filename)
                    if export.export_chat(self.chat, msgs, filename, self.db):
                        main.logstatus_flash("Exported %s.", filename)
                        util.start_file(filename)
                    else:
                        main.logstatus_flash("Error exporting to %s.", filename)
                        wx.MessageBox("Error exporting to \"%s\"." % filename,
                                      conf.Title, wx.OK | wx.ICON_WARNING)
                else:
                    wx.MessageBox("Current filter leaves no data to export.",
                                  conf.Title, wx.OK | wx.ICON_INFORMATION)
            finally:
                busy.Close()


    def on_size_html_stats(self, event):
        """Handler for sizing html_stats, sets new scroll position based
        previously stored one (HtmlWindow loses its scroll position on resize).
        """
        html = self.html_stats
        if hasattr(html, "_last_scroll_pos"):
            for i in range(2):
                orient = wx.VERTICAL if i else wx.HORIZONTAL
                # Division can be > 1 on first resizings, bound it to 1.
                ratio = min(1, util.safedivf(html._last_scroll_pos[i],
                    html._last_scroll_range[i]
                ))
                html._last_scroll_pos[i] = ratio * html.GetScrollRange(orient)
            # Execute scroll later as something resets it after this handler
            scroll_func = lambda: html and html.Scroll(*html._last_scroll_pos)
            wx.CallLater(50, scroll_func)
        event.Skip() # Allow event to propagate wx handler


    def on_select_participant(self, event):
        """
        Handler for selecting an item the in the participants list, toggles
        its checked state.
        """
        idx = event.GetIndex()
        if idx < self.list_participants.GetItemCount():
            c = self.list_participants.GetItem(idx)
            c.Check(not c.IsChecked())
            self.list_participants.SetItem(c)
            self.list_participants.Refresh() # Notify list of data change


    def on_scroll_grid_sql(self, event):
        """
        Handler for scrolling the SQL grid, seeks ahead if nearing the end of
        retrieved rows.
        """
        event.Skip()
        # Execute seek later, to give scroll position time to update
        wx.CallLater(50, self.seekahead_grid_sql)


    def seekahead_grid_sql(self):
        """Seeks ahead on the SQL grid if scroll position nearing the end."""
        SEEKAHEAD_POS_RATIO = 0.8
        scrollpos = self.grid_sql.GetScrollPos(wx.VERTICAL)
        scrollrange = self.grid_sql.GetScrollRange(wx.VERTICAL)
        if scrollpos > scrollrange * SEEKAHEAD_POS_RATIO:
            scrollpage = self.grid_sql.GetScrollPageSize(wx.VERTICAL)
            to_end = (scrollpos + scrollpage == scrollrange)
            # Seek to end if scrolled to the very bottom
            self.grid_sql.Table.SeekAhead(to_end)


    def on_scroll_html_stats(self, event):
        """
        Handler for scrolling the HTML stats, stores scroll position
        (HtmlWindow loses it on resize).
        """
        wx.CallAfter(self.store_html_stats_scroll)
        event.Skip() # Allow event to propagate wx handler


    def store_html_stats_scroll(self):
        """
        Stores the statistics HTML scroll position, needed for getting around
        its quirky scroll updating.
        """
        if not self:
            return
        self.html_stats._last_scroll_pos = [
            self.html_stats.GetScrollPos(wx.HORIZONTAL),
            self.html_stats.GetScrollPos(wx.VERTICAL)
        ]
        self.html_stats._last_scroll_range = [
            self.html_stats.GetScrollRange(wx.HORIZONTAL),
            self.html_stats.GetScrollRange(wx.VERTICAL)
        ]
        

    def on_click_html_stats(self, event):
        """
        Handler for clicking a link in chat history statistics, scrolls to
        anchor if anchor link, sorts the statistics if sort link, otherwise
        shows the history and finds the word clicked in the word cloud.
        """
        href = event.GetLinkInfo().Href
        if href.startswith("#") and self.html_stats.HasAnchor(href[1:]):
            self.html_stats.ScrollToAnchor(href[1:])
            wx.CallAfter(self.store_html_stats_scroll)
        elif href.startswith("file://"):
            filepath = urllib.url2pathname(href[5:])
            if filepath and os.path.exists(filepath):
                util.start_file(filepath)
            else:
                messageBox(
                    "The file \"%s\" cannot be found on this computer."
                    % (filepath),
                    conf.Title, wx.OK | wx.ICON_INFORMATION
                )
        elif href.startswith("sort://"):
            self.stats_sort_field = href[7:]
            self.populate_chat_statistics()
        else:
            self.stc_history.SearchBarVisible = True
            self.show_stats(False)
            self.stc_history.Search(href, flags=wx.stc.STC_FIND_WHOLEWORD)
            self.stc_history.SetFocusSearch()


    def on_click_searchall_result(self, event):
        """
        Handler for clicking a link in HtmlWindow, opens the link in default
        browser.
        """
        href = event.GetLinkInfo().Href
        link_data, tab_data = None, None
        if event.EventObject != self.label_html:
            tab_data = self.html_searchall.GetActiveTabData()
        if tab_data and tab_data.get("info"):
            link_data = tab_data["info"]["map"].get(href, {})
        if link_data or href.startswith("file://"):
            chat_id = link_data.get("chat")
            message_id = link_data.get("message")
            file = link_data.get("file")
            table_name, row = link_data.get("table"), link_data.get("row")
            if file or href.startswith("file://"):
                if file:
                    filename = file["filepath"] or file["filename"]
                    path = file["filepath"]
                else:
                    filename = path = filepath = urllib.url2pathname(href[5:])

                if path and os.path.exists(path):
                    util.start_file(path)
                else:
                    messageBox(
                        "The file \"%s\" cannot be found on this computer." %
                        filename, conf.Title, wx.OK | wx.ICON_INFORMATION)
            elif chat_id:
                self.notebook.SetSelection(self.pageorder[self.page_chats])
                c = next((c for c in self.chats if chat_id == c["id"]), None)
                if c:
                    self.load_chat(c, center_message_id=message_id)
                    self.show_stats(False)
                    self.stc_history.SetFocus()
            elif table_name and row:
                tableitem = None
                table_name = table_name.lower()
                table = next((t for t in self.db.get_tables()
                              if t["name"].lower() == table_name), None)
                item = self.tree_tables.GetNext(self.tree_tables.RootItem)
                while table and item and item.IsOk():
                    table2 = self.tree_tables.GetItemPyData(item)
                    if table2 and table2.lower() == table["name"].lower():
                        tableitem = item
                        break # break while item and itek.IsOk()
                    item = self.tree_tables.GetNextSibling(item)
                if tableitem:
                    self.notebook.SetSelection(self.pageorder[self.page_tables])
                    wx.GetApp().Yield()
                    # Only way to create state change in wx.gizmos.TreeListCtrl
                    class HackEvent(object):
                        def __init__(self, item): self._item = item
                        def GetItem(self):        return self._item
                    self.on_change_list_tables(HackEvent(tableitem))
                    if self.tree_tables.Selection != tableitem:
                        self.tree_tables.SelectItem(tableitem)
                        wx.GetApp().Yield()
                    grid = self.grid_table
                    if grid.Table.filters:
                        grid.Table.ClearSort(refresh=False)
                        grid.Table.ClearFilter()
                    # Search for matching row and scroll to it.
                    id_fields = [c["name"] for c in table["columns"]
                                 if c.get("pk_id")]
                    if not id_fields: # No primary key fields: take all
                        id_fields = [c["name"] for c in table["columns"]]
                    row_id = [row[c] for c in id_fields]
                    for i in range(grid.Table.GetNumberRows()):
                        row2 = grid.Table.GetRow(i)
                        row2_id = [row2[c] for c in id_fields]
                        if row_id == row2_id:
                            grid.MakeCellVisible(i, 0)
                            grid.SelectRow(i)
                            pagesize = grid.GetScrollPageSize(wx.VERTICAL)
                            pxls = grid.GetScrollPixelsPerUnit()
                            cell_coords = grid.CellToRect(i, 0)
                            y = cell_coords.y / (pxls[1] or 15)
                            x, y = 0, y - pagesize / 2
                            grid.Scroll(x, y)
                            break # break for i in range(self.grid_table..
        elif href.startswith("page:"):
            page = href[5:]
            if "#help" == page:
                html = self.html_searchall
                if html.GetTabDataByID(0):
                    html.SetActiveTabByID(0)
                else:
                    h = step.Template(templates.SEARCH_HELP_LONG).expand()
                    html.InsertTab(html.GetTabCount(), "Search help", 0,
                                   h, None)
            elif "#search" == page:
                self.edit_searchall.SetFocus()
            else:
                thepage = getattr(self, "page_" + page, None)
                if thepage:
                    self.notebook.SetSelection(self.pageorder[thepage])
        elif not (href.startswith("chat:") or href.startswith("message:")
        or href.startswith("file:")):
            webbrowser.open(href)


    def on_searchall_toggle_toolbar(self, event):
        """Handler for toggling a setting in search toolbar."""
        if wx.ID_INDEX == event.Id:
            conf.SearchInMessageBody = True
            conf.SearchInTables = False
            conf.SearchInChatInfo = conf.SearchInContacts = False
            self.label_search.Label = "&Search in messages:"
        elif wx.ID_ABOUT == event.Id:
            conf.SearchInChatInfo = True
            conf.SearchInTables = False
            conf.SearchInMessageBody = conf.SearchInContacts = False
            self.label_search.Label = "&Search in chat info:"
        elif wx.ID_PREVIEW == event.Id:
            conf.SearchInContacts = True
            conf.SearchInTables = False
            conf.SearchInMessageBody = conf.SearchInChatInfo = False
            self.label_search.Label = "&Search in contacts:"
        elif wx.ID_STATIC == event.Id:
            conf.SearchInTables = True
            conf.SearchInContacts = False
            conf.SearchInMessageBody = conf.SearchInChatInfo = False
            self.label_search.Label = "&Search in tables:"
        self.label_search.ContainingSizer.Layout()
        if wx.ID_NEW == event.Id:
            conf.SearchInNewTab = event.EventObject.GetToolState(event.Id)
        elif not event.EventObject.GetToolState(event.Id):
            # All others are radio tools and state might be toggled off by
            # shortkey key adapter
            event.EventObject.ToggleTool(event.Id, True)


    def on_searchall_stop(self, event):
        """
        Handler for clicking to stop a search, signals the search thread to
        close.
        """
        tab_data = self.html_searchall.GetActiveTabData()
        if tab_data and tab_data["id"] in self.workers_search:
            self.tb_search_settings.SetToolNormalBitmap(
                wx.ID_STOP, images.ToolbarStopped.Bitmap)
            self.workers_search[tab_data["id"]].stop_work(drop_results=True)
            self.workers_search[tab_data["id"]].stop()
            del self.workers_search[tab_data["id"]]


    def on_change_searchall_tab(self, event):
        """Handler for changing a tab in search window, updates stop button."""
        tab_data = self.html_searchall.GetActiveTabData()
        if tab_data and tab_data["id"] in self.workers_search:
            self.tb_search_settings.SetToolNormalBitmap(
                wx.ID_STOP, images.ToolbarStop.Bitmap)
        else:
            self.tb_search_settings.SetToolNormalBitmap(
                wx.ID_STOP, images.ToolbarStopped.Bitmap)


    def on_searchall_result(self, event):
        """
        Handler for getting results from search thread, adds the results to
        the search window.
        """
        result = event.result
        search_id, search_done = result.get("search", {}).get("id"), False
        tab_data = self.html_searchall.GetTabDataByID(search_id)
        if tab_data:
            tab_data["info"]["map"].update(result.get("map", {}))
            tab_data["info"]["partial_html"] += result.get("html", "")
            html = tab_data["info"]["partial_html"]
            if "done" in result:
                search_done = True
            else:
                html += "</table></font>"
            text = tab_data["info"]["text"]
            title = text[:50] + ".." if len(text) > 50 else text
            title += " (%s)" % result.get("count", 0)
            self.html_searchall.SetTabDataByID(search_id, title, html,
                                               tab_data["info"])
        if search_done:
            main.status_flash("Finished searching for \"%s\" in %s.",
                result["search"]["text"], self.db.filename
            )
            self.tb_search_settings.SetToolNormalBitmap(
                wx.ID_STOP, images.ToolbarStopped.Bitmap)
            if search_id in self.workers_search:
                self.workers_search[search_id].stop()
                del self.workers_search[search_id]
        if "error" in result:
            main.log("Error searching %s:\n\n%s", self.db, result["error"])
            msg = "Error searching %s:\n\n%s" % (
                  self.db, result.get("error_short", result["error"]))
            wx.MessageBox(msg, conf.Title, wx.OK | wx.ICON_WARNING)


    def on_searchall_callback(self, result):
        """Callback function for SearchThread, posts the data to self."""
        if self: # Check if instance is still valid (i.e. not destroyed by wx)
            wx.PostEvent(self, WorkerEvent(result=result))


    def on_search_contacts_callback(self, result):
        """Callback function for ContactSearchThread, posts the data to self."""
        if self: # Check if instance is still valid (i.e. not destroyed by wx)
            wx.PostEvent(self, ContactWorkerEvent(result=result))


    def on_searchall(self, event):
        """
        Handler for clicking to global search the database.
        """
        text = self.edit_searchall.Value
        if text.strip():
            main.status_flash("Searching for \"%s\" in %s.",
                              text, self.db.filename)
            html = self.html_searchall
            data = {"id": wx.NewId(), "db": self.db, "text": text, "map": {},
                    "window": html, "table": "", "partial_html": ""}
            fromtext = "" # "Searching for "text" in fromtext"
            if conf.SearchInMessageBody:
                data["table"] = "messages"
                fromtext = "messages"
            elif conf.SearchInChatInfo:
                data["table"] = "conversations"
                fromtext = "chat information"
            elif conf.SearchInContacts:
                data["table"] = "contacts"
                fromtext = "contact information"
            elif conf.SearchInTables:
                fromtext = data["table"] = "all tables"
            # Partially assembled HTML for current results
            template = step.Template(templates.SEARCH_HEADER_HTML)
            data["partial_html"] = template.expand(locals())

            worker = workers.SearchThread(self.on_searchall_callback)
            self.workers_search[data["id"]] = worker
            worker.work(data)
            bmp = images.ToolbarStop.Bitmap
            self.tb_search_settings.SetToolNormalBitmap(wx.ID_STOP, bmp)

            title = text[:50] + ".." if len(text) > 50 else text
            content = data["partial_html"] + "</table></font>"
            if conf.SearchInNewTab or not html.GetTabCount():
                html.InsertTab(0, title, data["id"], content, data)
            else:
                # Set new ID for the existing reused tab
                html.SetTabDataByID(html.GetActiveTabData()["id"], title,
                                    content, data, data["id"])

            self.notebook.SetSelection(self.pageorder[self.page_search])
            util.add_unique(conf.SearchHistory, text.strip(), 1,
                            conf.SearchHistoryMax)
            self.edit_searchall.SetChoices(conf.SearchHistory)
            self.edit_searchall.SetFocus()
            conf.save()


    def on_delete_tab_callback(self, tab):
        """
        Function called by html_searchall after deleting a tab, stops the
        ongoing search, if any.
        """
        tab_data = self.html_searchall.GetActiveTabData()
        if tab_data and tab_data["id"] == tab["id"]:
            self.tb_search_settings.SetToolNormalBitmap(
                wx.ID_STOP, images.ToolbarStopped.Bitmap)
        if tab["id"] in self.workers_search:
            self.workers_search[tab["id"]].stop()
            del self.workers_search[tab["id"]]


    def on_mouse_over_grid(self, event):
        """
        Handler for moving the mouse over a grid, shows datetime tooltip for
        UNIX timestamp cells.
        """
        tip = ""
        grid = event.EventObject.Parent
        prev_cell = getattr(grid, "_hovered_cell", None)
        x, y = grid.CalcUnscrolledPosition(event.X, event.Y)
        row, col = grid.XYToCell(x, y)
        if row >= 0 and col >= 0:
            value = grid.Table.GetValue(row, col)
            col_name = grid.Table.GetColLabelValue(col).lower()
            if type(value) is int and value > 100000000 \
            and ("time" in col_name or "history" in col_name):
                try:
                    tip = datetime.datetime.fromtimestamp(value).strftime(
                        "%Y-%m-%d %H:%M:%S")
                except:
                    tip = unicode(value)
            else:
                tip = unicode(value)
            tip = tip if len(tip) < 1000 else tip[:1000] + ".."
        if (row, col) != prev_cell or not (event.EventObject.ToolTip) \
        or event.EventObject.ToolTip.Tip != tip:
            event.EventObject.SetToolTipString(tip)
        grid._hovered_cell = (row, col)


    def on_participants_dclick(self, event):
        """
        Handler for double-clicking an item in the participants list,
        checks/unchecks (CheckListBox checks only when clicking on the
        checkbox icon itself).
        """
        checkeds = list(event.EventObject.Checked)
        do_check = not event.EventObject.IsChecked(event.Selection)
        if do_check and event.Selection not in checkeds:
            checkeds.append(event.Selection)
        elif not do_check and event.Selection in checkeds:
            checkeds.remove(event.Selection)
        event.EventObject.SetChecked(checkeds)


    def on_filterreset_chat(self, event):
        """
        Handler for clicking to reset current chat history filter, restores
        initial values to filter controls.
        """
        for i in range(self.list_participants.GetItemCount()):
            c = self.list_participants.GetItem(i)
            c.Check(True)
            self.list_participants.SetItem(c)
        self.edit_filtertext.Value = ""
        self.range_date.SetValues(*self.chat_filter["startdaterange"])
        self.list_participants.Refresh()


    def on_filter_chat(self, event):
        """
        Handler for clicking to filter current chat history, applies the
        current filter to the chat messages.
        """
        new_filter, old_filter = self.build_filter(), self.stc_history.Filter
        current_filter = dict((t, old_filter) for t in new_filter)
        self.current_filter = current_filter
        self.new_filter = new_filter
        if new_filter != current_filter:
            self.chat_filter.update(new_filter)
            busy = controls.BusyPanel(self, "Filtering messages.")
            try:
                self.stc_history.SetFilter(self.chat_filter)
                self.stc_history.RefreshMessages()
                self.populate_chat_statistics()
            finally:
                busy.Close()
            has_messages = self.chat["message_count"] > 0
            self.tb_chat.EnableTool(wx.ID_MORE, has_messages)


    def build_filter(self):
        """Builds chat filter data from current control state."""
        # At least one participant must be selected: reset to previously
        # selected participants instead if nothing selected
        reselecteds = []
        for i in range(self.list_participants.GetItemCount()):
            # UltimateListCtrl does not expose checked state, have to
            # query it from each individual row
            if not self.list_participants.GetItem(i).IsChecked():
                identity = self.list_participants.GetItemData(i)["identity"]
                if identity in self.chat_filter["participants"]:
                    reselecteds.append(i)
        if reselecteds:
            for i in range(self.list_participants.GetItemCount()):
                identity = self.list_participants.GetItemData(i)["identity"]
                if identity in reselecteds:
                    c = self.list_participants.GetItem(i)
                    c.Check(True)
                    self.list_participants.SetItem(i, c)
            self.list_participants.Refresh()
        participants = []
        for i in range(self.list_participants.GetItemCount()):
            if self.list_participants.IsItemChecked(i):
                identity = self.list_participants.GetItemData(i)["identity"]
                participants.append(identity)
        filterdata = {
            "daterange": self.range_date.Values,
            "text": self.edit_filtertext.Value,
            "participants": participants
        }
        return filterdata


    def on_toggle_filter(self, event):
        """Handler for clicking to show/hide chat filter."""
        self.toggle_filter(not self.splitter_stc.IsSplit())


    def on_toggle_stats(self, event):
        """
        Handler for clicking to show/hide statistics for chat, toggles display
        between chat history window and statistics window.
        """
        html, stc = self.html_stats, self.stc_history
        self.show_stats(not html.Shown)
        (html if html.Shown else stc).SetFocus()


    def on_toggle_maximize(self, event):
        """Handler for toggling to maximize chat window and hide chat list."""
        splitter = self.splitter_chats
        if splitter.IsSplit():
            splitter._sashPosition = splitter.SashPosition
            splitter.Unsplit(self.panel_chats1)
            shorthelp = "Restore chat panel to default size  (Alt-M)"
        else:
            pos = getattr(splitter, "_sashPosition", self.Size[1] / 3)
            splitter.SplitHorizontally(self.panel_chats1, self.panel_chats2,
                                       sashPosition=pos)
            shorthelp = "Maximize chat panel  (Alt-M)"
        self.tb_chat.SetToolShortHelp(wx.ID_ZOOM_100, shorthelp)


    def toggle_filter(self, on):
        """Toggles the chat filter panel on/off."""
        if self.splitter_stc.IsSplit() and not on:
            self.splitter_stc._sashPosition = self.splitter_stc.SashPosition
            self.splitter_stc.Unsplit(self.panel_stc2)
        elif not self.splitter_stc.IsSplit() and on:
            p = getattr(self.splitter_stc, "_sashPosition",
                self.splitter_stc.Size.width - self.panel_stc2.BestSize.width)
            self.splitter_stc.SplitVertically(self.panel_stc1, self.panel_stc2,
                                              sashPosition=p)
            list_participants = self.list_participants
            list_participants.SetColumnWidth(0, list_participants.Size.width)


    def show_stats(self, show=True):
        """Shows or hides the statistics window."""
        html, stc = self.html_stats, self.stc_history
        changed = False
        focus = False
        for i in [html, stc]:
            focus = focus or (i.Shown and i.FindFocus() == i)
        if not stc.Shown != show:
            stc.Show(not show)
            changed = True
        if html.Shown != show:
            html.Show(show)
            changed = True
        if changed:
            stc.ContainingSizer.Layout()
        if focus: # Switch focus to the other control if previous had focus
            (html if show else stc).SetFocus()
        if show:
            if hasattr(html, "_last_scroll_pos"):
                html.Scroll(*html._last_scroll_pos)
            elif html.HasAnchor(html.OpenedAnchor):
                html.ScrollToAnchor(html.OpenedAnchor)
        self.tb_chat.ToggleTool(wx.ID_PROPERTIES, show)


    def on_button_reset_grid(self, event):
        """
        Handler for clicking to remove sorting and filtering on a grid,
        resets the grid and its view.
        """
        grid = self.grid_table \
            if event.EventObject == self.button_reset_grid_table \
            else self.grid_sql
        if grid.Table:
            grid.Table.ClearSort(refresh=False)
            grid.Table.ClearFilter()
            grid.ContainingSizer.Layout() # React to grid size change


    def on_button_export_grid(self, event):
        """
        Handler for clicking to export wx.Grid contents to file, allows the
        user to select filename and type and creates the file.
        """
        grid_source = self.grid_table
        sql = ""
        table = ""
        if event.EventObject is self.button_export_sql:
            grid_source = self.grid_sql
            sql = getattr(self, "last_sql", "")
        if grid_source.Table:
            if grid_source is self.grid_table:
                table = grid_source.Table.table.capitalize()
                namebase = "table \"%s\"" % table
                self.dialog_savefile.Wildcard = \
                    "HTML document (*.html)|*.html|" \
                    "SQL INSERT statements (*.sql)|*.sql|" \
                    "CSV spreadsheet (*.csv)|*.csv"
            else:
                namebase = "SQL query"
                self.dialog_savefile.Wildcard = \
                    "HTML document (*.html)|*.html|" \
                    "CSV spreadsheet (*.csv)|*.csv"
                grid_source.Table.SeekAhead(True)
            default = "Skype - %s" % namebase
            self.dialog_savefile.Filename = util.safe_filename(default)
            self.dialog_savefile.Message = "Save table as"
            if wx.ID_OK == self.dialog_savefile.ShowModal():
                filename = self.dialog_savefile.GetPath()
                exts = ["html", "csv"] if grid_source is not self.grid_table \
                       else ["html", "sql", "csv"]
                extname = exts[self.dialog_savefile.FilterIndex]
                if not filename.lower().endswith(".%s" % extname):
                    filename += ".%s" % extname
                busy = controls.BusyPanel(
                       self, "Exporting \"%s\"." % filename)
                main.status("Exporting \"%s\".", filename)
                try:
                    export_result = export.export_grid(grid_source,
                        filename, default, self.db, sql, table)
                finally:
                    busy.Close()
                if export_result:
                    main.logstatus_flash("Exported %s.", filename)
                    util.start_file(filename)
                else:
                    main.logstatus_flash("Error exporting to %s.", filename)
                    wx.MessageBox("Error exporting to \"%s\"." % filename,
                                  conf.Title, wx.OK | wx.ICON_WARNING)


    def on_keydown_sql(self, event):
        """
        Handler for pressing a key in SQL editor, listens for Alt-Enter and
        executes the currently selected line, or currently active line.
        """
        stc = event.GetEventObject()
        if event.AltDown() and wx.WXK_RETURN == event.KeyCode:
            sql = (stc.SelectedText or stc.CurLine[0]).strip()
            if sql:
                self.execute_sql(sql)
        event.Skip() # Allow to propagate to other handlers


    def on_button_sql(self, event):
        """
        Handler for clicking to run an SQL query, runs the query, displays its
        results, if any, and commits changes done, if any.
        """
        sql = self.stc_sql.Text.strip()
        if sql:
            self.execute_sql(sql)


    def execute_sql(self, sql):
        """Executes the SQL query and populates the SQL grid with results."""
        try:
            grid_data = None
            if sql.lower().startswith(("select", "pragma", "explain")):
                # SELECT statement: populate grid with rows
                grid_data = self.db.execute_select(sql)
                self.grid_sql.SetTable(grid_data)
                self.button_export_sql.Enabled = True
            else:
                # Assume action query
                affected_rows = self.db.execute_action(sql)
                self.grid_sql.SetTable(None)
                self.grid_sql.CreateGrid(1, 1)
                self.grid_sql.SetColLabelValue(0, "Affected rows")
                self.grid_sql.SetCellValue(0, 0, str(affected_rows))
                self.button_export_sql.Enabled = False
            main.logstatus_flash("Executed SQL \"%s\".", sql)
            size = self.grid_sql.Size
            self.grid_sql.Fit()
            # Jiggle size by 1 pixel to refresh scrollbars
            self.grid_sql.Size = size[0], size[1]-1
            self.grid_sql.Size = size[0], size[1]
            self.last_sql = sql
            self.grid_sql.SetColMinimalAcceptableWidth(100)
            if grid_data:
                col_range = range(grid_data.GetNumberCols())
                map(self.grid_sql.AutoSizeColLabelSize, col_range)
        except Exception, e:
            wx.MessageBox(
                unicode(e).capitalize(), conf.Title, wx.OK | wx.ICON_WARNING)


    def on_change_table(self, event):
        """
        Handler when table grid data is changed, refreshes icons,
        table lists and database display.
        """
        grid_data = self.grid_table.Table
        # Enable/disable commit and rollback icons
        self.tb_grid.EnableTool(wx.ID_SAVE, grid_data.IsChanged())
        self.tb_grid.EnableTool(wx.ID_UNDO, grid_data.IsChanged())
        # Highlight changed tables in the table list
        item = self.tree_tables.GetNext(self.tree_tables.RootItem)
        while item and item.IsOk():
            list_table = self.tree_tables.GetItemPyData(item)
            if list_table:
                list_table = list_table.lower()
                if list_table == grid_data.table:
                    self.tree_tables.SetItemTextColour(
                        item,
                        conf.DBTableChangedColour if grid_data.IsChanged()
                        else "black")
                    break # break while
            item = self.tree_tables.GetNextSibling(item)

        # Mark database as changed/pristine in the parent notebook tabs
        for i in range(self.parent_notebook.GetPageCount()):
            if self.parent_notebook.GetPage(i) == self:
                title = self.Label + ("*" if grid_data.IsChanged() else "")
                if self.parent_notebook.GetPageText(i) != title:
                    self.parent_notebook.SetPageText(i, title)
                break


    def on_commit_table(self, event):
        """Handler for clicking to commit the changed database table."""
        if wx.OK == wx.MessageBox(
            "Are you sure you want to commit these changes (%s)?" % (
                self.grid_table.Table.GetChangedInfo()
            ),
            conf.Title, wx.OK | wx.CANCEL | wx.ICON_QUESTION
        ):
            self.grid_table.Table.SaveChanges()
            self.on_change_table(None)
            # Refresh tables list with updated row counts
            tablemap = dict((t["name"], t) for t in self.db.get_tables(True))
            item = self.tree_tables.GetNext(self.tree_tables.RootItem)
            while item and item.IsOk():
                table = self.tree_tables.GetItemPyData(item)
                if table:
                    self.tree_tables.SetItemText(item, "%d row%s" % (
                        tablemap[table]["rows"],
                        "s" if tablemap[table]["rows"] != 1 else " "
                    ), 1)
                    if table == self.grid_table.Table.table:
                        self.tree_tables.SetItemTextColour(
                            item,
                            conf.DBTableChangedColour
                            if self.grid_table.Table.IsChanged() else "black")
                item = self.tree_tables.GetNextSibling(item)


    def on_rollback_table(self, event):
        """Handler for clicking to rollback the changed database table."""
        self.grid_table.Table.UndoChanges()
        self.on_change_table(None)
        # Refresh scrollbars; without CallAfter wx 2.8 can crash
        wx.CallAfter(self.grid_table.ContainingSizer.Layout)


    def on_insert_row(self, event):
        """
        Handler for clicking to insert a table row, lets the user edit a new
        grid line.
        """
        self.grid_table.InsertRows(0)
        self.grid_table.SetGridCursor(0, 0)
        self.grid_table.ScrollLineY = 0 # Scroll to top to the new row
        self.grid_table.Refresh()
        self.on_change_table(None)
        # Refresh scrollbars; without CallAfter wx 2.8 can crash
        wx.CallAfter(self.grid_table.ContainingSizer.Layout)


    def on_delete_row(self, event):
        """
        Handler for clicking to delete a table row, removes the row from grid.
        """
        selected_rows = self.grid_table.GetSelectedRows()
        cursor_row = self.grid_table.GetGridCursorRow()
        if cursor_row >= 0:
            selected_rows.append(cursor_row)
        for row in selected_rows:
            self.grid_table.DeleteRows(row)
        self.grid_table.ContainingSizer.Layout() # Refresh scrollbars
        self.on_change_table(None)


    def on_update_grid_table(self, event):
        """Refreshes the table grid UI components, like toolbar icons."""
        self.tb_grid.EnableTool(wx.ID_SAVE, self.grid_table.Table.IsChanged())
        self.tb_grid.EnableTool(wx.ID_UNDO, self.grid_table.Table.IsChanged())


    def on_change_list_tables(self, event):
        """
        Handler for selecting an item in the tables list, loads the table data
        into the table grid.
        """
        table = None
        item = event.GetItem()
        if item and item.IsOk():
            table = self.tree_tables.GetItemPyData(item)
            lower = table.lower() if table else None
        if table and \
        (not self.grid_table.Table
         or self.grid_table.Table.table.lower() != lower):
            i = self.tree_tables.GetNext(self.tree_tables.RootItem)
            while i:
                text = self.tree_tables.GetItemText(i).lower()
                bgcolour = conf.DBTableOpenedColour if (text == table) \
                           else "white"
                self.tree_tables.SetItemBackgroundColour(i, bgcolour)
                i = self.tree_tables.GetNextSibling(i)
            busy = controls.BusyPanel(self, "Loading table \"%s\"." % table)
            grid_data = self.db.get_table_data(table)
            self.label_table.Label = "Table \"%s\":" % table
            self.grid_table.SetTable(grid_data)
            self.page_tables.Layout() # React to grid size change
            self.grid_table.Scroll(0, 0)
            self.grid_table.SetColMinimalAcceptableWidth(100)
            col_range = range(grid_data.GetNumberCols())
            map(self.grid_table.AutoSizeColLabelSize, col_range)
            self.on_change_table(None)
            self.tb_grid.EnableTool(wx.ID_ADD, True)
            self.tb_grid.EnableTool(wx.ID_DELETE, True)
            self.button_export_table.Enabled = True
            busy.Close()


    def on_change_list_chats(self, event):
        """
        Handler for selecting an item in the chats list, loads the
        messages into the message log.
        """
        self.load_chat(self.list_chats.GetItemMappedData(event.Index))


    def load_chat(self, chat, center_message_id=None):
        """Loads history of the specified chat (as returned from db)."""
        if chat and (chat != self.chat or center_message_id):
            busy = None
            if chat != self.chat:
                # Update chat list colours and scroll to the opened chat
                self.list_chats.Freeze()
                scrollpos = self.list_chats.GetScrollPos(wx.VERTICAL)
                index_selected = -1
                for i in range(self.list_chats.ItemCount):
                    if self.list_chats.GetItemMappedData(i) == self.chat:
                        self.list_chats.SetItemBackgroundColour(
                            i, self.list_chats.BackgroundColour)
                    elif self.list_chats.GetItemMappedData(i) == chat:
                        index_selected = i
                        self.list_chats.SetItemBackgroundColour(
                            i, conf.ListOpenedBgColour)
                if index_selected >= 0:
                    delta = index_selected - scrollpos
                    if delta < 0 or abs(delta) >= self.list_chats.CountPerPage:
                        nudge = -self.list_chats.CountPerPage / 2
                        self.list_chats.ScrollLines(delta + nudge)
                self.list_chats.Thaw()
                wx.GetApp().Yield(True) # Allow display to refresh
                # Add shortcut key flag to chat label
                self.label_chat.Label = chat["title_long"].replace(
                    "chat", "&chat"
                ).replace("Chat", "&Chat") + ":"

            dates_range  = [None, None] # total available date range
            dates_values = [None, None] # currently filtered date range
            if chat != self.chat or (center_message_id
            and not self.stc_history.IsMessageShown(center_message_id)):
                busy = controls.BusyPanel(self, "Loading history for %s."
                                              % chat["title_long_lc"])
                self.db.get_conversations_stats([chat]) # Refresh last messages
                self.edit_filtertext.Value = self.chat_filter["text"] = ""
                date_range = [
                    chat["first_message_datetime"].date()
                    if chat["first_message_datetime"] else None,
                    chat["last_message_datetime"].date()
                    if chat["last_message_datetime"] else None
                ]
                self.chat_filter["daterange"] = date_range
                self.chat_filter["startdaterange"] = date_range
                dates_range = dates_values = date_range
                avatar_default = images.AvatarDefault.Bitmap
                if chat != self.chat:
                    # If chat has changed, load avatar images for the contacts
                    self.list_participants.ClearAll()
                    self.list_participants.InsertColumn(0, "")
                    sz_avatar = conf.AvatarImageSize
                    il = wx.ImageList(*sz_avatar)
                    il.Add(avatar_default)
                    self.list_participants.AssignImageList(
                        il, wx.IMAGE_LIST_SMALL)
                    index = 0
                    # wx will open a warning dialog on image error otherwise
                    nolog = wx.LogNull()
                    for p in chat["participants"]:
                        b = 0
                        if not p["contact"].get("avatar_bitmap"):
                            bmp = skypedata.get_avatar(p["contact"], sz_avatar)
                            if bmp:
                                p["contact"]["avatar_bitmap"] = bmp
                        if "avatar_bitmap" in p["contact"]:
                            b = il.Add(p["contact"]["avatar_bitmap"])
                        self.list_participants.InsertImageStringItem(
                            index, p["contact"]["name"], b, it_kind=1)
                        c = self.list_participants.GetItem(index)
                        c.Check(True)
                        self.list_participants.SetItem(c)
                        self.list_participants.SetItemData(index, p)
                        index += 1
                    del nolog # Restore default wx message logger
                    self.list_participants.SetColumnWidth(0, wx.LIST_AUTOSIZE)
                self.chat_filter["participants"] = [
                    p["identity"] for p in chat["participants"]]

            if center_message_id and self.chat == chat:
                if not self.stc_history.IsMessageShown(center_message_id):
                    self.stc_history.SetFilter(self.chat_filter)
                    self.stc_history.RefreshMessages(center_message_id)
                else:
                    self.stc_history.FocusMessage(center_message_id)
            else:
                self.stc_history.SetFilter(self.chat_filter)
                self.stc_history.Populate(chat, self.db,
                    center_message_id=center_message_id
                )
            if self.stc_history.GetMessage(0):
                values = [self.stc_history.GetMessage(0)["datetime"],
                    self.stc_history.GetMessage(-1)["datetime"]
                ]
                dates_values = tuple(i.date() for i in values)
                if not filter(None, dates_range):
                    dates_range2 = list(dates_range)
                    dates_range = [
                        chat["first_message_datetime"].date()
                        if chat["first_message_datetime"] else None,
                        chat["last_message_datetime"].date()
                        if chat["last_message_datetime"] else None
                    ]
                if not filter(None, dates_range):
                    dates_range = dates_values
                self.chat_filter["daterange"] = dates_range
                self.chat_filter["startdaterange"] = dates_values
            self.range_date.SetRange(*dates_range)
            self.range_date.SetValues(*dates_values)
            has_messages = bool(self.stc_history.GetMessage(0))
            self.tb_chat.EnableTool(wx.ID_MORE, has_messages)
            if not self.chat:
                # Very first load, toggle filter tool button on
                self.tb_chat.ToggleTool(wx.ID_MORE, self.splitter_stc.IsSplit())
            if self.chat != chat:
                self.chat = chat
            if busy:
                busy.Close()
            self.panel_chats2.Enabled = True
            self.populate_chat_statistics()
            if self.html_stats.Shown:
                self.show_stats(True) # To restore scroll position


    def populate_chat_statistics(self):
        """Populates html_stats with chat statistics and word cloud."""
        stats_html = self.stc_history.GetStatisticsHtml(self.stats_sort_field)
        if stats_html:
            fs, fn = self.memoryfs, "avatar__default.jpg"
            if fn not in fs["files"]:
                bmp = images.AvatarDefault.Bitmap
                fs["handler"].AddFile(fn, bmp, wx.BITMAP_TYPE_BMP)
                fs["files"][fn] = 1

            for p in self.chat["participants"]:
                if "avatar_bitmap" in p["contact"]:
                    vals = (self.db.filename.encode("utf-8"), p["identity"])
                    fn = "%s_%s.jpg" % tuple(map(urllib.quote, vals))
                    if fn not in fs["files"]:
                        bmp = p["contact"]["avatar_bitmap"]
                        fs["handler"].AddFile(fn, bmp, wx.BITMAP_TYPE_BMP)
                        fs["files"][fn] = 1

        previous_anchor = self.html_stats.OpenedAnchor
        previous_scrollpos = getattr(self.html_stats, "_last_scroll_pos", None)
        self.html_stats.Freeze()
        self.html_stats.SetPage(stats_html)
        if previous_scrollpos:
            self.html_stats.Scroll(*previous_scrollpos)
        elif previous_anchor and self.html_stats.HasAnchor(previous_anchor):
            self.html_stats.ScrollToAnchor(previous_anchor)
        self.html_stats.Thaw()


    def on_sort_grid_column(self, event):
        """
        Handler for clicking a table grid column, sorts table by the column.
        """
        grid = event.GetEventObject()
        if grid.Table:
            row, col = event.GetRow(), event.GetCol()
            # Remember scroll positions, as grid update loses them
            scroll_hor = grid.GetScrollPos(wx.HORIZONTAL)
            scroll_ver = grid.GetScrollPos(wx.VERTICAL)
            if row < 0: # Only react to clicks in the header
                grid.Table.SortColumn(col)
            grid.ContainingSizer.Layout() # React to grid size change
            grid.Scroll(scroll_hor, scroll_ver)


    def on_filter_grid_column(self, event):
        """
        Handler for right-clicking a table grid column, lets the user
        change the column filter.
        """
        grid = event.GetEventObject()
        if grid.Table:
            row, col = event.GetRow(), event.GetCol()
            # Remember scroll positions, as grid update loses them
            scroll_hor = grid.GetScrollPos(wx.HORIZONTAL)
            scroll_ver = grid.GetScrollPos(wx.VERTICAL)
            if row < 0: # Only react to clicks in the header
                grid_data = grid.Table
                current_filter = unicode(grid_data.filters[col]) \
                                 if col in grid_data.filters else ""
                dialog = wx.TextEntryDialog(self,
                    "Filter column \"%s\" by:" % grid_data.columns[col]["name"],
                    "Filter", defaultValue=current_filter,
                    style=wx.OK | wx.CANCEL)
                if wx.ID_OK == dialog.ShowModal():
                    new_filter = dialog.GetValue()
                    if len(new_filter):
                        busy = controls.BusyPanel(self.page_tables,
                            "Filtering column \"%s\" by \"%s\"." % (
                                grid_data.columns[col]["name"], new_filter
                        ))
                        grid_data.AddFilter(col, new_filter)
                        busy.Close()
                    else:
                        grid_data.RemoveFilter(col)
            grid.ContainingSizer.Layout() # React to grid size change


    def load_data(self):
        """Loads data from our SkypeDatabase."""
        self.label_title.Label = "Database \"%s\":" % self.db

        try:
            # Populate the chats list
            self.chats = self.db.get_conversations()
            for c in self.chats:
                c["people"] = "" # Set empty data, stats will come later

            column_map = [
                ("title", "Chat"), ("message_count", "Messages"),
                ("created_datetime", "Created"),
                ("first_message_datetime", "First message"),
                ("last_message_datetime", "Last message"),
                ("type_name", "Type"), ("people", "People")
            ]
            frmt = lambda r, c: r[c].strftime("%Y-%m-%d %H:%M") if r[c] else ""
            formatters = {"created_datetime": frmt,
                          "first_message_datetime": frmt,
                          "last_message_datetime": frmt, }
            self.list_chats.Populate(column_map, self.chats, formatters)
            self.list_chats.SortListItems(4, 1) # Sort by last message datetime
            self.list_chats.OnSortOrderChanged()
            # Chat name column can be really long, pushing all else out of view
            self.list_chats.SetColumnWidth(0, 300)

            wx.CallLater(200, self.load_later_data)

            last_search = conf.LastSearchResults.get(self.db.filename)
            if last_search:
                title = last_search.get("title", "")
                html = last_search.get("content", "")
                info = last_search.get("info")
                tabid = wx.NewId() if 0 != last_search.get("id") else 0
                self.html_searchall.InsertTab(0, title, tabid, html, info)
                self.edit_searchall.Value = info.get("text", "")
        except Exception, e:
            wx.CallAfter(self.update_tabheader)
            main.logstatus_flash("Could not load chat list from %s.\n\n%s",
                                 self.db, traceback.format_exc())
            wx.MessageBox("Could not load chat list from %s.\n\nError: %s." %
                          (self.db, e), conf.Title, wx.OK | wx.ICON_WARNING)


    def update_tabheader(self):
        """Updates page tab header with option to close page."""
        if self:
            self.ready_to_close = True
        if self:
            self.TopLevelParent.update_notebook_header()


    def load_later_data(self):
        """
        Loads later data from the database, like table metainformation and
        statistics for all chats, used as a background callback to speed
        up page opening.
        """
        try:
            tables = self.db.get_tables()
            # Fill table tree with information on row counts and columns
            self.tree_tables.DeleteAllItems()
            root = self.tree_tables.AddRoot("SQLITE")
            child = None
            for table in tables:
                child = self.tree_tables.AppendItem(root, table["name"])
                self.tree_tables.SetItemText(child, "%d row%s" % (
                    table["rows"], "s" if table["rows"] != 1 else " "
                ), 1)
                self.tree_tables.SetItemPyData(child, table["name"])

                for col in self.db.get_table_columns(table["name"]):
                    grandchld = self.tree_tables.AppendItem(child, col["name"])
                    self.tree_tables.SetItemText(grandchld, col["type"], 1)
            self.tree_tables.Expand(root)
            if child:
                self.tree_tables.Expand(child)
                self.tree_tables.SetColumnWidth(0, -1)
                self.tree_tables.SetColumnWidth(1, -1)
                self.tree_tables.Collapse(child)

            # Add table and column names to SQL editor autocomplete
            self.stc_sql.AutoCompAddWords([t["name"] for t in tables])
            for t in tables:
                coldata = self.db.get_table_columns(t["name"])
                fields = [c["name"] for c in coldata]
                self.stc_sql.AutoCompAddSubWords(t["name"], fields)

            # Load chat statistics and update the chat list
            self.db.get_conversations_stats(self.chats)
            for c in self.chats:
                people = sorted([p["identity"] for p in c["participants"]])
                if skypedata.CHATS_TYPE_SINGLE != c["type"]:
                    c["people"] = "%s (%s)" % (len(people), ", ".join(people))
                else:
                    people = [p for p in people if p != self.db.id]
                    c["people"] = ", ".join(people)
            self.list_chats.RefreshItems()
            # Some columns can be really long, pushing all else out of view
            widths = self.list_chats.GetColumnWidths()
            for i, w in [(i, w) for i, w in enumerate(widths) if w > 300]:
                self.list_chats.SetColumnWidth(i, 300)

            if self.chat:
                # If the user already opened a chat while later data
                # was loading, update the date range control values.
                date_range = [
                    self.chat["first_message_datetime"].date()
                    if self.chat["first_message_datetime"] else None,
                    self.chat["last_message_datetime"].date()
                    if self.chat["last_message_datetime"] else None
                ]
                self.range_date.SetRange(*date_range)
            self.update_file_info()
        except Exception, e:
            if self:
                msg = "Error loading additional data from %s.\n\n%s" % (
                      self.db, traceback.format_exc())
                main.log(msg)
                wx.MessageBox(msg, conf.Title, wx.OK | wx.ICON_WARNING)
        if self:
            main.status_flash("Opened Skype database %s.", self.db)
            wx.CallAfter(self.update_tabheader)



class MergerPage(wx.Panel):
    """
    A wx.Notebook page for comparing two Skype databases, has its own Notebook
    with one page for diffing/merging chats, and another for contacts.
    """


    """Labels for chat diff result in chat list."""
    DIFFSTATUS_IDENTICAL = "In sync"
    DIFFSTATUS_DIFFERENT = "Out of sync"

    def __init__(self, parent_notebook, db1, db2, title):
        wx.Panel.__init__(self, parent=parent_notebook)
        self.pageorder = {} # {page: notebook index, }
        self.parent_notebook = parent_notebook
        self.ready_to_close = False
        self.is_merging = False # Whether merging is currently underway
        self.db1 = db1
        self.db2 = db2
        main.status("Opening Skype databases %s and %s.", self.db1, self.db2)
        self.db1.register_consumer(self)
        self.db2.register_consumer(self)
        self.Label = title
        parent_notebook.InsertPage(1, self, title)
        busy = controls.BusyPanel(
            self, "Comparing \"%s\"\n and \"%s\"." % (db1, db2)
        )

        self.chat_diff = None      # Chat currently being diffed
        self.chat_diff_data = None # {"messages": [,], "participants": [,]}
        self.compared = None       # List of all chats
        self.con1difflist = None   # Contact and contact group differences
        self.con2difflist = None   # Contact and contact group differences
        self.con1diff = None       # Contact differences for left
        self.con2diff = None       # Contact differences for right
        self.congroup1diff = None  # Contact group differences for left
        self.congroup2diff = None  # Contact group differences for right
        self.chats_differing = None  # [[chats differing in db1], [in db2]]
        self.diffresults_html = None # [[diff results HTML for db1], [for db2]]
        self.diffresults_info = None # [{contact, group, chat, message}, ]
        self.contacts_column_map = [
            ("identity", "Account"), ("name", "Name"),
            ("phone_mobile_normalized", "Mobile phone"),
            ("country", "Country"), ("city", "City"), ("about", "About"),
            ("__type", "Type")
        ]
        self.chats_column_map = [
            ("title", "Chat"), ("messages1", "Messages in left"),
            ("messages2", "Messages in right"),
            ("last_message_datetime1", "Last message in left"),
            ("last_message_datetime2", "Last message in right"),
            ("type_name", "Type"), ("diff_status", "First glance"),
            ("people", "People"),
        ]
        self.Bind(EVT_WORKER, self.on_worker_merge_result)
        self.worker_merge = workers.MergeThread(self.on_worker_merge_callback)

        sizer = self.Sizer = wx.BoxSizer(wx.VERTICAL)

        sizer_header = wx.BoxSizer(wx.HORIZONTAL)
        label = self.html_dblabel = wx.html.HtmlWindow(parent=self,
            size=(-1, 36), style=wx.html.HW_SCROLLBAR_NEVER)
        label.SetFonts(normal_face=self.Font.FaceName,
                       fixed_face=self.Font.FaceName, sizes=[8] * 7)
        self.Bind(wx.html.EVT_HTML_LINK_CLICKED, self.on_link_db, label)
        button_swap = self.button_swap = \
            wx.Button(parent=self, label="&Swap left-right")
        button_swap.Enabled = False
        button_swap.SetToolTipString("Swaps left and right database.")
        self.Bind(wx.EVT_BUTTON, self.on_swap, button_swap)
        sizer_header.Add(label, border=5, proportion=1,
                         flag=wx.GROW | wx.TOP | wx.BOTTOM)
        sizer_header.Add(button_swap, border=5,
                         flag=wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(sizer_header, flag=wx.GROW)
        sizer.Layout() # To avoid header moving around during page creation

        bookstyle = wx.lib.agw.fmresources.INB_LEFT
        if (wx.version().startswith("2.8") and sys.version.startswith("2.")
        and sys.version[:5] < "2.7.3"):
            # wx 2.8 + Python below 2.7.3: labelbook can partly cover tab area
            bookstyle |= wx.lib.agw.fmresources.INB_FIT_LABELTEXT
        notebook = self.notebook = wx.lib.agw.labelbook.FlatImageBook(
            parent=self, agwStyle=bookstyle,
            style=wx.BORDER_STATIC)

        il = wx.ImageList(32, 32)
        idx1 = il.Add(images.PageMergeAll.Bitmap)
        idx2 = il.Add(images.PageMergeChats.Bitmap)
        idx3 = il.Add(images.PageContacts.Bitmap)
        notebook.AssignImageList(il)

        self.create_page_merge_all(notebook)
        self.create_page_merge_chats(notebook)
        self.create_page_merge_contacts(notebook)

        notebook.SetPageImage(0, idx1)
        notebook.SetPageImage(1, idx2)
        notebook.SetPageImage(2, idx3)

        sizer.Add(notebook, proportion=10, border=5,
                  flag=wx.GROW | wx.LEFT | wx.RIGHT | wx.BOTTOM)

        self.TopLevelParent.page_merge_latest = self
        self.TopLevelParent.console.run(
            "page12 = self.page_merge_latest # Merger tab")
        self.TopLevelParent.console.run(
            "db1, db2 = page12.db1, page12.db2 # Chosen Skype databases")

        # Layout() required, otherwise sizers do not start working
        # automatically as it's late creation
        self.Layout()
        self.Refresh()
        self.load_data()
        busy.Close()
        wx.CallAfter(self.page_merge_all.Layout)


    def create_page_merge_all(self, notebook):
        """Creates a page for merging everything at once."""
        page = self.page_merge_all = wx.Panel(parent=notebook)
        self.pageorder[page] = len(self.pageorder)
        notebook.AddPage(page, "Merge all")
        page.Sizer = wx.BoxSizer(wx.VERTICAL)
        panel = wx.Panel(page, style=wx.BORDER_STATIC)
        panel.BackgroundColour = wx.WHITE
        sizer = panel.Sizer = wx.BoxSizer(wx.VERTICAL)
        page.Sizer.Add(panel, proportion=1, border=5, flag=wx.GROW | wx.LEFT)
        sizer_data = wx.BoxSizer(wx.HORIZONTAL)
        sizer_html = wx.BoxSizer(wx.HORIZONTAL)
        sizer_html1 = wx.BoxSizer(wx.VERTICAL)
        sizer_html2 = wx.BoxSizer(wx.VERTICAL)
        label1 = self.label_all1 = wx.StaticText(panel, style=wx.ALIGN_RIGHT,
            label="%s\n\nAnalyzing..%s" % (self.db1.filename, "\n" * 7))
        label2 = self.label_all2 = wx.StaticText(panel,
            label="%s\n\nAnalyzing..%s" % (self.db2.filename, "\n" * 7))
        button_scan = self.button_scan_all = controls.NoteButton(panel,
            label="Scan differences between left and right",
            note="Go through all chat messages in either database and find "
            "ones not present in the other.\nThis can take several minutes.",
            bmp=images.ButtonCompare.Bitmap, style=wx.ALIGN_CENTER)
        button_scan.BackgroundColour = wx.WHITE
        button_scan.MinSize = 700, -1

        html1 = self.html_results1 = controls.ScrollingHtmlWindow(
            panel, style=wx.BORDER_SUNKEN)
        html2 = self.html_results2 = controls.ScrollingHtmlWindow(
            panel, style=wx.BORDER_SUNKEN)
        for h in [html1, html2]:
            h.SetFonts(normal_face=self.Font.FaceName,
                fixed_face=self.Font.FaceName, sizes=[8] * 7)
            h.Bind(wx.html.EVT_HTML_LINK_CLICKED, self.on_click_htmldiff)
            h.BackgroundColour = conf.MergeHtmlBackgroundColour

        buttonall1 = self.button_mergeall1 = controls.NoteButton(
            panel, label="Merge differences to the right",
            bmp=images.ButtonMergeLeft.Bitmap, style=wx.ALIGN_RIGHT)
        buttonall2 = self.button_mergeall2 = controls.NoteButton(panel,
            label="Merge differences to the left",
            bmp=images.ButtonMergeRight.Bitmap)
        buttonall1.BackgroundColour = buttonall2.BackgroundColour = wx.WHITE

        button_scan.Enabled = False
        buttonall1.Enabled = buttonall2.Enabled = False
        button_scan.Bind(wx.EVT_BUTTON, self.on_scan_all)
        buttonall1.Bind(wx.EVT_BUTTON, self.on_merge_all)
        buttonall2.Bind(wx.EVT_BUTTON, self.on_merge_all)
        sizer_data.Add(label1, proportion=1, border=5, flag=wx.ALL)
        sizer_data.AddSpacer(20)
        sizer_data.Add(label2, proportion=1, border=5, flag=wx.ALL)
        sizer.AddSpacer(20)
        sizer.Add(sizer_data, flag=wx.ALIGN_CENTER)
        sizer.AddSpacer(10)
        sizer.Add(button_scan, flag=wx.ALIGN_CENTER)
        panel_gauge_scan = self.panel_gauge_scan = wx.Panel(panel)
        panel_gauge_scan.BackgroundColour = panel.BackgroundColour
        panel_gauge_scan.Sizer = wx.BoxSizer(wx.VERTICAL)
        self.label_gauge_scan = wx.StaticText(panel_gauge_scan, label="")
        self.gauge_scan = wx.Gauge(panel_gauge_scan, size=(300, 15),
                                   style=wx.GA_HORIZONTAL | wx.PD_SMOOTH)
        self.gauge_scan.ForegroundColour = conf.GaugeColour
        panel_gauge_scan.Sizer.Add(self.label_gauge_scan, flag=wx.ALIGN_CENTER)
        panel_gauge_scan.Sizer.Add(self.gauge_scan, flag=wx.ALIGN_CENTER)
        sizer.Add(panel_gauge_scan, flag=wx.GROW)
        self.panel_gauge_scan.Hide()

        sizer_html1.Add(html1, proportion=1, flag=wx.GROW)
        sizer_html1.Add(buttonall1, border=15,
                        flag=wx.TOP | wx.ALIGN_RIGHT | wx.GROW)
        sizer_html2.Add(html2, proportion=1, flag=wx.GROW)
        sizer_html2.Add(buttonall2, border=15, flag=wx.TOP | wx.GROW)
        sizer_html.Add(sizer_html1, proportion=1, border=5,
                       flag=wx.ALL | wx.GROW)
        sizer_html.Add(sizer_html2, proportion=1, border=5,
                       flag=wx.ALL | wx.GROW)
        sizer.Add(sizer_html, proportion=1, border=10,
                  flag=wx.LEFT | wx.TOP | wx.RIGHT | wx.GROW)

        panel_gauge_merge = self.panel_gauge_merge = wx.Panel(panel)
        panel_gauge_merge.BackgroundColour = panel.BackgroundColour
        panel_gauge_merge.Sizer = wx.BoxSizer(wx.VERTICAL)
        self.label_gauge_merge = wx.StaticText(panel_gauge_merge, label="")
        self.gauge_merge = wx.Gauge(panel_gauge_merge, size=(300, 15),
                                  style=wx.GA_HORIZONTAL | wx.PD_SMOOTH)
        self.gauge_merge.ForegroundColour = conf.GaugeColour
        panel_gauge_merge.Sizer.Add(self.label_gauge_merge, flag=wx.ALIGN_CENTER)
        panel_gauge_merge.Sizer.Add(self.gauge_merge, flag=wx.ALIGN_CENTER)
        sizer.Add(panel_gauge_merge, border=10, flag=wx.BOTTOM | wx.GROW)
        self.panel_gauge_merge.Hide()


    def create_page_merge_chats(self, notebook):
        """Creates a page for seeing and merging differing chats."""
        page = self.page_merge_chats = wx.Panel(parent=notebook)
        self.pageorder[page] = len(self.pageorder)
        notebook.AddPage(page, "Chats")
        sizer = page.Sizer = wx.BoxSizer(wx.VERTICAL)
        splitter = self.splitter_merge = wx.SplitterWindow(
            parent=page, style=wx.BORDER_NONE
        )
        splitter.SetMinimumPaneSize(50)
        panel1 = wx.Panel(parent=splitter)
        panel2 = wx.Panel(parent=splitter)
        sizer1 = panel1.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer2 = panel2.Sizer = wx.BoxSizer(wx.VERTICAL)

        sizer1.Add(wx.StaticText(parent=panel1, label="&Chat comparison:"))
        list_chats = self.list_chats = controls.SortableListView(
            parent=panel1, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        list_chats.Enabled = False
        self.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_change_list_chats,
                  list_chats)
        sizer1.Add(list_chats, proportion=1, flag=wx.GROW)

        label_chat = self.label_merge_chat = \
            wx.StaticText(parent=panel2, label="")
        splitter_diff = self.splitter_diff = wx.SplitterWindow(
            parent=panel2, style=wx.BORDER_SIMPLE)
        splitter_diff.SetMinimumPaneSize(1)
        panel_stc1 = self.panel_stc1 = wx.Panel(parent=splitter_diff)
        panel_stc2 = self.panel_stc2 = wx.Panel(parent=splitter_diff)
        sizer_stc1 = panel_stc1.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer_stc2 = panel_stc2.Sizer = wx.BoxSizer(wx.VERTICAL)

        label_db1 = self.label_chat_db1 = wx.StaticText(
            parent=panel_stc1, label="")
        stc1 = self.stc_diff1 = ChatContentSTC(
            parent=panel_stc1, style=wx.BORDER_STATIC)
        label_db2 = self.label_chat_db2 = wx.StaticText(
            parent=panel_stc2, label="")
        stc2 = self.stc_diff2 = ChatContentSTC(
            parent=panel_stc2, style=wx.BORDER_STATIC)

        button1 = self.button_merge_chat_1 = wx.Button(parent=panel_stc1,
            label="Merge messages to database on the right >>>")
        button2 = self.button_merge_chat_2 = wx.Button(parent=panel_stc2,
            label="<<< Merge messages to database on the left")
        button1.Bind(wx.EVT_BUTTON, self.on_merge_chat)
        button2.Bind(wx.EVT_BUTTON, self.on_merge_chat)
        button1.Enabled = button2.Enabled = False

        sizer_stc1.Add(label_db1, border=5, flag=wx.ALL)
        sizer_stc1.Add(stc1, proportion=1, flag=wx.GROW)
        sizer_stc1.Add(button1, border=5, flag=wx.ALIGN_CENTER | wx.ALL)
        sizer_stc2.Add(label_db2, border=5, flag=wx.ALL)
        sizer_stc2.Add(stc2, proportion=1, flag=wx.GROW)
        sizer_stc2.Add(button2, border=5, flag=wx.ALIGN_CENTER | wx.ALL)
        sizer2.Add(label_chat, border=5, flag=wx.TOP | wx.LEFT)
        sizer2.Add(splitter_diff, proportion=1, flag=wx.GROW)

        sizer.AddSpacer(10)
        sizer.Add(splitter, border=5, proportion=1, flag=wx.GROW | wx.ALL)
        splitter_diff.SetSashGravity(0.5)
        splitter_diff.SplitVertically(panel_stc1, panel_stc2)
        sash_pos = self.Size[1] / 3
        splitter.SplitHorizontally(panel1, panel2, sashPosition=sash_pos)


    def create_page_merge_contacts(self, notebook):
        """Creates a page for seeing and merging differing contacts."""
        page = self.page_merge_contacts = wx.Panel(parent=notebook)
        self.pageorder[page] = len(self.pageorder)
        notebook.AddPage(page, "Contacts")
        sizer = page.Sizer = wx.BoxSizer(wx.VERTICAL)

        splitter = self.splitter_contacts = wx.SplitterWindow(
            parent=page, style=wx.BORDER_NONE
        )
        splitter.SetMinimumPaneSize(1)
        panel1 = wx.Panel(parent=splitter)
        panel2 = wx.Panel(parent=splitter)
        sizer1 = panel1.Sizer = wx.BoxSizer(wx.VERTICAL)
        sizer2 = panel2.Sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(parent=page, label="&Differing contacts:")
        list1 = self.list_contacts1 = controls.SortableListView(
            parent=panel1, size=(700, 300), style=wx.LC_REPORT)
        self.Bind(
            wx.EVT_LIST_ITEM_SELECTED, self.on_select_list_contacts, list1)
        self.Bind(
            wx.EVT_LIST_ITEM_DESELECTED, self.on_select_list_contacts, list1)
        list2 = self.list_contacts2 = controls.SortableListView(
            parent=panel2, size=(700, 300), style=wx.LC_REPORT)
        self.Bind(
            wx.EVT_LIST_ITEM_SELECTED, self.on_select_list_contacts, list2)
        self.Bind(
            wx.EVT_LIST_ITEM_DESELECTED, self.on_select_list_contacts, list2)

        button1 = self.button_merge_contacts1 = wx.Button(parent=panel1,
            label="Merge selected contact(s) to database on the right >>>")
        button2 = self.button_merge_contacts2 = wx.Button(parent=panel2,
            label="<<< Merge selected contact(s) to database on the left")
        button_all1 = self.button_merge_allcontacts1 = wx.Button(parent=panel1,
            label="Merge all contacts to database on the right >>>")
        button_all2 = self.button_merge_allcontacts2 = wx.Button(parent=panel2,
            label="<<< Merge all contacts to database on the left")
        button1.Bind(wx.EVT_BUTTON, self.on_merge_contacts)
        button2.Bind(wx.EVT_BUTTON, self.on_merge_contacts)
        button_all1.Bind(wx.EVT_BUTTON, self.on_merge_contacts)
        button_all2.Bind(wx.EVT_BUTTON, self.on_merge_contacts)
        button1.Enabled = button2.Enabled = False
        button_all1.Enabled = button_all2.Enabled = False
        sizer1.Add(list1, proportion=1, flag=wx.GROW)
        sizer1.Add(button1, border=5, flag=wx.ALIGN_CENTER | wx.ALL)
        sizer1.Add(button_all1, border=5, flag=wx.ALIGN_CENTER | wx.ALL)
        sizer2.Add(list2, proportion=1, flag=wx.GROW)
        sizer2.Add(button2, border=5, flag=wx.ALIGN_CENTER | wx.ALL)
        sizer2.Add(button_all2, border=5, flag=wx.ALIGN_CENTER | wx.ALL)

        sash_pos = self.Size.width / 2
        splitter.SplitVertically(panel1, panel2, sashPosition=sash_pos)

        sizer.AddSpacer(10)
        sizer.Add(label, border=5, flag=wx.BOTTOM | wx.LEFT)
        sizer.Add(splitter, proportion=1, border=5,
                  flag=wx.GROW | wx.LEFT | wx.RIGHT)



    def split_panels(self):
        """
        Splits all SplitterWindow panels. To be called after layout in
        Linux wx 2.8, as otherwise panels do not get sized properly.
        """
        if not self:
            return
        sash_pos = self.Size[1] / 3
        panel1, panel2 = self.splitter_merge.Children
        self.splitter_merge.Unsplit()
        self.splitter_merge.SplitHorizontally(panel1, panel2, sash_pos)
        sash_pos = self.page_merge_contacts.Size[0] / 2
        panel1, panel2 = self.splitter_contacts.Children
        self.splitter_contacts.Unsplit()
        self.splitter_contacts.SplitVertically(panel1, panel2, sash_pos)


    def on_click_htmldiff(self, event):
        """
        Handler for clicking a chat link in diff overview, opens the chat
        comparison in Chats page.
        """
        href = event.GetLinkInfo().Href
        chat = None
        for c in self.compared:
            if c["identity"] == href:
                chat = c
                break # break for c in self.compared
        if chat and self.page_merge_chats.Enabled:
            self.on_change_list_chats(chat=chat)
            self.notebook.SetSelection(self.pageorder[self.page_merge_chats])


    def on_worker_merge_result(self, event):
        """Handler for worker_merge result callback, updates UI and texts."""
        if "merge" == event.result.get("type"):
            self.on_merge_all_result(event)
        elif "diff" == event.result.get("type"):
            self.on_scan_all_result(event)
            

    def on_worker_merge_callback(self, result):
        """Callback function for MergeThread, posts the data to self."""
        if self: # Check if instance is still valid (i.e. not destroyed by wx)
            wx.PostEvent(self, WorkerEvent(result=result))


    def on_swap(self, event):
        """
        Handler for clicking to swap left and right databases, changes data
        structures and UI content.
        """
        self.db1, self.db2 = self.db2, self.db1
        f1, f2 = self.db1.filename, self.db2.filename
        label_title = "Database %s vs %s:" % (
            util.htmltag("a", attrs={"href": f1}, content=f1, utf=False),
            util.htmltag("a", attrs={"href": f2}, content=f2, utf=False))
        self.html_dblabel.SetPage(label_title)
        self.html_dblabel.BackgroundColour = self.label_all1.BackgroundColour
        self.con1diff, self.con2diff = self.con2diff, self.con1diff
        self.con1difflist, self.con2difflist = \
            self.con2difflist, self.con1difflist
        self.congroup1diff, self.congroup2diff = \
            self.congroup1diff, self.congroup2diff

        # Swap button and label texts
        for a, b in [(self.button_mergeall1, self.button_mergeall2)]:
            a.Note, b.Note = b.Note, a.Note
        for a, b in [(self.label_chat_db1, self.label_chat_db2),
                     (self.label_all1, self.label_all2)]:
            a.Label, b.Label = b.Label, a.Label

        def get_item_index(sizer, item):
            if hasattr(sizer, "GetItemIndex"):
                return sizer.GetItemIndex(item)
            # wx 2.8 has no wx.Sizer.GetItemIndex
            for i, c in enumerate(sizer.Children):
                if c.GetWindow() == item:
                    return i

        # Swap complex controls in parents and sizers.
        for name in ["stc_diff", "html_results"]:
            o1, o2 = getattr(self, name + "1"), getattr(self, name + "2")
            sizer1, sizer2 = o1.ContainingSizer, o2.ContainingSizer
            parent1, parent2 = o1.Parent, o2.Parent
            idx1, idx2 = get_item_index(sizer1, o1), get_item_index(sizer2, o2)
            item1, item2 = sizer1.GetItem(idx1), sizer2.GetItem(idx2)
            sizer1.Remove(idx1)
            if sizer1 == sizer2: idx2 -= 1
            sizer2.Remove(idx2)
            if sizer1 == sizer2: idx2 += 1
            if parent1 != parent2:
                o1.Reparent(parent2), o2.Reparent(parent1)
            sizer1.Insert(idx1, o2, proportion=item1.Proportion,
                          flag=item1.Flag, border=item1.Border)
            sizer2.Insert(idx2, o1, proportion=item2.Proportion,
                          flag=item2.Flag, border=item2.Border)
            setattr(self, name + "1", o2), setattr(self, name + "2", o1)

        # Swap left and right in data structures.
        for lst in [self.compared, self.con1diff, self.con2diff,
                    self.congroup1diff, self.congroup2diff]:
            for item in lst or []:
                for t in ["c", "messages", "g"]:
                    key1, key2 = "%s1" % t, "%s2" % t
                    if key1 in item and key2 in item:
                        item[key1], item[key2] = item[key2], item[key1]
        if self.chats_differing:
            self.chats_differing = self.chats_differing[::-1]
        if self.diffresults_html:
            self.diffresults_html = self.diffresults_html[::-1]
        if self.diffresults_info:
            self.diffresults_info = self.diffresults_info[::-1]

        # Repopulate chat and contact lists
        if self.con1difflist is not None:
            self.list_contacts1.Populate(
                self.contacts_column_map, self.con1difflist)
        if self.con2difflist is not None:
            self.list_contacts2.Populate(
                self.contacts_column_map, self.con2difflist)
        if self.compared is not None:
            self.list_chats.Populate(self.chats_column_map, self.compared)
            for i, c in enumerate(self.compared):
                if self.DIFFSTATUS_IDENTICAL == c["diff_status"]:
                    self.list_chats.SetItemTextColour(
                        i, conf.DiffIdenticalColour)
            self.list_chats.SortListItems(3, 0) # Sort by last message in left
            self.list_chats.OnSortOrderChanged()

        # Swap button states
        for i in ["all", "_chat_", "_allcontacts"]:
            n = "button_merge%s" % i
            b1, b2 = getattr(self, "%s1" % n), getattr(self, "%s2" % n)
            b1.Enabled, b2.Enabled = b2.Enabled, b1.Enabled
        self.button_merge_contacts1.Enabled = False # Selection and ordering
        self.button_merge_contacts2.Enabled = False # in lists is lost anyway

        self.Refresh()
        self.page_merge_all.Layout()
        self.Layout()


    def on_link_db(self, event):
        """Handler on clicking a database link, opens the database tab."""
        self.TopLevelParent.load_database_page(event.GetLinkInfo().Href)


    def on_select_list_contacts(self, event):
        list_source = event.EventObject
        button_target = self.button_merge_contacts1
        if list_source is self.list_contacts2:
            button_target = self.button_merge_contacts2
        button_target.Enabled = list_source.SelectedItemCount > 0


    def on_click_link(self, event):
        """
        Handler for clicking a link in chat history, opens the link in system
        browser.
        """
        stc = event.EventObject
        if stc.GetStyleAt(event.Position) == self.stc_styles["link"]:
            # Go back and forth from position and get URL range.
            url_range = {-1: -1, 1: -1} # { start and end positions }
            for step in url_range:
                pos = event.Position
                while stc.GetStyleAt(pos + step) == self.stc_styles["link"]:
                    pos += step
                url_range[step] = pos
            url = stc.GetTextRange(url_range[-1], url_range[1] + 1)
            #print url, url_range
            webbrowser.open(url)


    def on_change_list_chats(self, event=None, chat=None):
        """
        Handler for activating an item in the differing chats list,
        goes through all messages of the chat in both databases and shows
        those messages that are missing or different, for both left and
        right.
        """
        c = chat if event is None \
            else self.list_chats.GetItemMappedData(event.Index)
        if not self.chat_diff or c["identity"] != self.chat_diff["identity"]:
            self.label_merge_chat.Label = "Messages different in %s:" \
                % c["title_long_lc"]
            self.label_chat_db1.Label = self.db1.filename
            self.label_chat_db2.Label = self.db2.filename
            scrollpos = self.list_chats.GetScrollPos(wx.VERTICAL)
            index_selected = -1
            for i in range(self.list_chats.ItemCount):
                if self.list_chats.GetItemMappedData(i) == self.chat_diff:
                    self.list_chats.SetItemBackgroundColour(
                        i, self.list_chats.BackgroundColour
                    )
                elif self.list_chats.GetItemMappedData(i) == c:
                    index_selected = i
                    self.list_chats.SetItemBackgroundColour(
                        i, conf.ListOpenedBgColour
                    )
            if index_selected >= 0:
                delta = index_selected - scrollpos
                if delta < 0 or abs(delta) >= self.list_chats.CountPerPage:
                    nudge = -self.list_chats.CountPerPage / 2
                    self.list_chats.ScrollLines(delta + nudge)
            busy = controls.BusyPanel(self,
                   "Diffing messages for %s." % c["title_long_lc"])

            data1, data2 = None, None
            if self.chats_differing: # Check for already calculated cached diff
                data1 = next((c1 for c1 in self.chats_differing[0]
                              if c1["chat"]["id"] == c["id"]), None)
                data2 = next((c2 for c2 in self.chats_differing[1]
                              if c2["chat"]["id"] == c["id"]), None)
            if data1 or data2:
                diff = (data1 or data2)["diff"]
            else:
                diff = self.worker_merge.get_chat_diff(c, self.db1, self.db2)

            for i, ids in filter(lambda x: x[1], enumerate(diff["messages"])):
                diff["messages"][i] = []
                db = [self.db1, self.db2][i]
                for ids2 in [ids[j:j+999] for j in range(0, len(ids), 999)]:
                    # Divide into chunks, as SQLite can take up to 999 parameters.
                    sql = "id IN (%s)" % ", ".join("?%s" % (j+1) for j in range(len(ids2)))
                    params = dict(("%s" % (j+1), x) for j, x in enumerate(ids2))
                    diff["messages"][i] += db.get_messages(additional_sql=sql,
                                               additional_params=params)
            self.chat_diff = c
            self.chat_diff_data = diff
            self.button_merge_chat_1.Enabled = len(diff["messages"][0])
            self.button_merge_chat_2.Enabled = len(diff["messages"][1])
            busy.Close()

            self.stc_diff1.Populate(c, self.db1, diff["messages"][0])
            self.stc_diff2.Populate(c, self.db2, diff["messages"][1])


    def on_merge_contacts(self, event):
        """
        Handler for clicking to merge contacts from one database to the other,
        either selected or all contacts, depending on button clicked.
        """
        button = event.EventObject
        db_target, db_source = self.db2, self.db1
        list_source = self.list_contacts1
        source = 0
        if button in \
        [self.button_merge_allcontacts2, self.button_merge_contacts2]:
            db_target, db_source = self.db1, self.db2
            list_source = self.list_contacts2
            source = 1
        button_all = [
            self.button_merge_allcontacts1, self.button_merge_allcontacts2
        ][source]
        contacts, contactgroups, indices = [], [], []
        if button is button_all:
            for i in range(list_source.ItemCount):
                data = list_source.GetItemMappedData(i)
                if "Contact" == data["__type"]:
                    contacts.append(data["__data"])
                else:
                    contactgroups.append(data["__data"])
                indices.append(i)
        else:
            selected = list_source.GetFirstSelected()
            while selected >= 0:
                data = list_source.GetItemMappedData(selected)
                if "Contact" == data["__type"]:
                    contacts.append(data["__data"])
                else:
                    contactgroups.append(data["__data"])
                indices.append(selected)
                selected = list_source.GetNextSelected(selected)
        # Contacts and contact groups are shown in the same list. If a contact
        # group is chosen, it can include contacts not yet in target database.
        contacts_target_final = dict([(c["identity"], c) for c in contacts])
        contacts_target_final.update(
            dict([(c["identity"], c) for c in db_target.get_contacts()])
        )
        contacts_source = dict([(c['identity'], c)
            for c in db_source.get_contacts()
        ])
        for group in contactgroups:
            members = set(group["members"].split(" "))
            for new in members.difference(contacts_target_final):
                contacts.append(contacts_source[new])
        text_add = ""
        if contacts:
            text_add += util.plural("contact", contacts)
        if contactgroups:
            text_add += (" and " if contacts else "") \
                       + util.plural("contact group", contactgroups)
        if (contacts or contactgroups) and wx.OK == wx.MessageBox(
                "Copy %s\nfrom %s\ninto %s?" % (text_add, self.db1, self.db2),
                conf.Title, wx.OK | wx.CANCEL | wx.ICON_QUESTION):
            self.is_merging = True
            try:
                if contacts:
                    db_target.insert_contacts(contacts, db_source)
                if contactgroups:
                    db_target.replace_contactgroups(contactgroups, db_source)
            finally:
                self.is_merging = False
            for i in sorted(indices)[::-1]:
                list_source.DeleteItem(i)
            condiff = [self.con1diff, self.con2diff][source]
            cgdiff = [self.congroup1diff, self.congroup1diff][source]
            difflist = [self.con1difflist, self.con1difflist][source]
            for c in [contacts, contactgroups]:
                [l.remove(c) for l in [condiff, cgdiff, difflist] if c in l]
            button_all.Enabled = list_source.ItemCount > 0
            wx.MessageBox("Copied %s\nfrom %s\ninto %s?" % (text_add,
                self.db1, self.db2), conf.Title, wx.OK | wx.ICON_INFORMATION
            )
            db_target.clear_cache()


    def on_scan_all(self, event):
        """
        Handler for clicking to scan all differences to copy to the other
        database, collects the data and loads it to screen.
        """
        main.logstatus("Scanning differences between %s and %s.",
                       self.db1, self.db2)
        self.chats_differing = [[], []]
        self.diffresults_html = ["", ""]
        self.diffresults_info = [collections.defaultdict(int),
                                 collections.defaultdict(int)]
        self.button_scan_all.Enabled = False
        self.button_swap.Enabled = False
        self.button_mergeall1.Enabled = self.button_mergeall2.Enabled = False
        self.button_mergeall1.Note = self.button_mergeall2.Note = ""
        self.panel_gauge_merge.Hide()
        self.update_gauge(self.gauge_scan, 0, "Scanning contacts.")
        for i in range(2):
            text = "<body bgcolor='%s'>" % conf.MergeHtmlBackgroundColour
            contacts = [self.con1diff, self.con2diff][i]
            contactgroups = [self.congroup1diff, self.congroup2diff][i]
            if contacts:
                # Add skypenames where contact have same names.
                names, names2 = sorted((c["name"], c) for c in contacts), []
                counts = collections.defaultdict(int)
                for c in names: counts[c[0]] += 1
                names2 = [(c[0] if counts[c[0]] < 2 
                          else "%s (%s)" % (c[0], c[1]["skypename"]))
                          for c in names]
                text += "%s: %s.<br />" % (
                        util.plural("new contact", contacts), ", ".join(names2))
                self.diffresults_info[i]["contact"] += len(contacts)
            if contactgroups:
                text += util.plural("new contact group", contactgroups) + \
                        ".<br />"
                self.diffresults_info[i]["contact group"] += len(contactgroups)
            self.diffresults_html[i] = text
            html = [self.html_results1, self.html_results2][i]
            html.SetPage(text)
            if self.diffresults_info[i]:
                button = [self.button_mergeall1, self.button_mergeall2][i]
                note, inter = "", ""
                for field in ["contact", "contact group"]:
                    value = self.diffresults_info[i][field]
                    if value:
                        note += inter + util.plural(field, value)
                        inter = ", "
                button.Note = note + "." if note else ""
        params = {"db1": self.db1, "db2": self.db2, "type": "diff"}
        self.worker_merge.work(params)


    def on_scan_all_result(self, event):
        """
        Handler for getting diff results from worker thread, adds the results
        to the diff windows.
        """
        result = event.result
        for i in range(2):
            self.diffresults_html[i] += result["htmls"][i]
            self.chats_differing[i].extend(result["chats"][i])
            info = self.diffresults_info[i]
            if result["chats"][i]:
                info["chat"] += len(result["chats"][i])
            for data in result["chats"][i]:
                if data["diff"]["messages"]:
                    info["message"] += len(data["diff"]["messages"][i])
            if result["htmls"][i]:
                html = [self.html_results1, self.html_results2][i]
                html.Freeze()
                scrollpos = html.GetScrollPos(wx.VERTICAL)
                html.SetPage(self.diffresults_html[i])
                html.Scroll(0, scrollpos)
                html.Thaw()
                button = [self.button_mergeall1, self.button_mergeall2][i]
                note, inter = "", ""
                for field in ["chat", "message", "contact", "contact group"]:
                    value = info[field]
                    if value:
                        note += inter + util.plural(field, value)
                        inter = ", "
                button.Note = note + "." if note else ""
            i, count = result["index"] + 1, len(self.compared)
            percent = math.ceil(100 * util.safedivf(i, count))
            msg = "Scan %d%% complete (%s of %s)." % \
                  (percent, i + 1, util.plural("conversation", count))
            self.update_gauge(self.gauge_scan, percent, msg)
        if "done" in result:
            s1 = util.plural("differing chat", self.chats_differing[0])
            s2 = util.plural("differing chat", self.chats_differing[1])
            main.logstatus_flash("Found %s in %s and %s in %s.",
                                 s1, self.db1, s2, self.db2)
            self.button_mergeall1.Enabled = bool(self.button_mergeall1.Note)
            self.button_mergeall2.Enabled = bool(self.button_mergeall2.Note)
            self.button_swap.Enabled = True
            for i in [i for i in range(2) if not self.diffresults_info[i]]:
                h = [self.html_results1, self.html_results2][i]
                h.SetPage("<body bgcolor='%s'>No new messages or contacts."
                          "</body>" % conf.MergeHtmlBackgroundColour)
            self.update_gauge(self.gauge_scan, 100, "Scan completed.")


    def on_merge_all(self, event):
        """
        Handler for clicking to copy all the differences to the other
        database, asks for final confirmation and executes.
        """
        source = 0 if event.EventObject == self.button_mergeall1 else 1
        db1 = [self.db1, self.db2][source]
        db2 = [self.db1, self.db2][1 - source]
        chats  = self.chats_differing[source]
        contacts = [self.con1diff, self.con2diff][source]
        contactgroups = [self.congroup1diff, self.congroup2diff][source]
        # Contacts and contact groups are shown in the same list. If a contact
        # group is chosen, it can include contacts not yet in target database.
        contacts_target_final = dict([(c["identity"], c) for c in contacts])
        contacts_target_final.update(
            dict([(c["identity"], c) for c in db2.get_contacts()])
        )
        contacts_source = dict((c['identity'], c)
                               for c in db1.get_contacts())
        for group in contactgroups:
            members = set(group["members"].split(" "))
            for new in members.difference(contacts_target_final):
                contacts.append(contacts_source[new])
        info = ""
        if chats:
            info += util.plural("chat", chats)
        if contacts:
            info += (" and " if info else "") \
                 + util.plural("contact", contacts)
        if contactgroups:
            info += (" and " if info else "") \
                 + util.plural("contact group", contactgroups)
        if wx.OK == wx.MessageBox(
            "Copy data of %s\nfrom %s\ninto %s?" % (info, db1, db2),
            conf.Title, wx.OK | wx.CANCEL | wx.ICON_QUESTION
        ):
            self.panel_gauge_scan.Hide()
            self.update_gauge(self.gauge_merge, 0, "Merge 0% complete.")
            self.button_swap.Enabled = self.button_scan_all.Enabled = False
            self.button_mergeall1.Enabled = False
            self.button_mergeall2.Enabled = False
            self.page_merge_chats.Enabled = False
            self.page_merge_contacts.Enabled = False
            params = locals()
            params["type"] = "merge"
            main.logstatus("Merging %s from %s to %s.", info, db1, db2)
            self.worker_merge.work(params)
            self.is_merging = True


    def on_merge_all_result(self, event):
        """
        Handler for getting merge results from worker thread, refreshes texts
        and UI controls.
        """
        result = event.result
        if "index" in result:
            i, count = result["index"] + 1, len(result["params"]["chats"])
            percent = math.ceil(100 * util.safedivf(i, count))
            msg = "Merge %d%% complete (%s of %s)." % \
                  (percent, i, util.plural("conversation", count))
            self.update_gauge(self.gauge_merge, percent, msg)
        if "error" in result:
            self.update_gauge(self.gauge_merge, 0, "Merge error.")
            main.log("Error merging chats.\n\n%s", result["error"])
            msg = "Error merging chats.\n\n%s" % (
                  result.get("error_short", result["error"]))
            wx.MessageBox(msg, conf.Title, wx.OK | wx.ICON_WARNING)
        if "done" in result:
            self.is_merging = False
            self.page_merge_chats.Enabled = True
            self.page_merge_contacts.Enabled = True
            source, info = result["source"], result["info"]
            db1 = [self.db1, self.db2][source]
            db2 = [self.db1, self.db2][1 - source]
            db2.clear_cache()
            button = [self.button_mergeall1, self.button_mergeall2][source]
            for s in [self.stc_diff1, self.stc_diff2]:
                s.SetReadOnly(False)
                s.ClearAll()
                s.SetReadOnly(True)
            self.list_chats.ClearAll()
            self.list_contacts1.ClearAll()
            self.list_contacts2.ClearAll()
            self.label_merge_chat.Label = ""
            self.label_chat_db1.Label = ""
            self.label_chat_db2.Label = ""
            self.chat_diff = None
            self.diffresults_html[source] = []
            if "error" not in result:
                h = [self.html_results1, self.html_results2][source]
                h.SetPage("<body bgcolor='%s'>%s merged to %s.</body>" %
                    (conf.MergeHtmlBackgroundColour, button.Note[:-1], db2))
                main.logstatus_flash("Merged %s from %s to %s.", info, db1, db2)
                self.update_gauge(self.gauge_merge, 100, "Merge completed.")
                wx.MessageBox("Copied %s\nfrom %s\ninto %s." % (info, db1, db2),
                              conf.Title, wx.OK | wx.ICON_INFORMATION)
            self.button_swap.Enabled = self.button_scan_all.Enabled = True
            button.Note = ""
            self.button_mergeall1.Enabled = bool(self.button_mergeall1.Note)
            self.button_mergeall2.Enabled = bool(self.button_mergeall2.Note)
            wx.CallLater(20, self.load_later_data)


    def update_gauge(self, gauge, value, message=""):
        """
        Updates the gauge value and message on page_merge_all. If value is
        None, hides gauge panel.
        """
        if value is None:
            gauge.Hide()
        else:
            gauge.Value = value
            for c in gauge.Parent.Children:
                if isinstance(c, wx.StaticText):
                    c.Label = message
                    break # break for c in gauge..
            gauge.Parent.Sizer.Layout()
            if not gauge.Parent.Shown:
                gauge.Parent.Show()
                gauge.Parent.ContainingSizer.Layout()


    def on_merge_chat(self, event):
        """
        Handler for clicking to merge a chat from either side db to the other
        side db.
        """
        source = 0 if (self.button_merge_chat_1 == event.EventObject) else 1
        db1 = [self.db1, self.db2][source]
        db2 = [self.db1, self.db2][1 - source]
        chats_differing = self.chats_differing[source] \
                          if self.chats_differing else []
        chat  = [self.chat_diff["c1"], self.chat_diff["c2"]][source]
        chat2 = [self.chat_diff["c1"], self.chat_diff["c2"]][1 - source]
        messages, messages2 = \
            self.chat_diff_data["messages"][::[1, -1][source]]
        participants, participants2 = \
            self.chat_diff_data["participants"][::[1, -1][source]]
        condiff = [self.con1diff, self.con2diff][source]
        stc = [self.stc_diff1, self.stc_diff2][source]
        contacts2 = []

        if messages or participants:
            info = ""
            parts = []
            new_chat = not chat2
            newstr = "" if new_chat else "new "
            if new_chat:
                info += "new chat with "
            if messages:
                parts.append(util.plural("%smessage" % newstr, messages))
            if participants:
                # Add to contacts those that are new
                cc2 = [db1.id, db2.id] + \
                    [i["identity"] for i in db2.get_contacts()]
                contacts2 = [i["contact"] for i in participants
                    if "id" in i["contact"] and i["identity"] not in cc2]
                if contacts2:
                    parts.append(util.plural("new contact", contacts2))
                parts.append(util.plural("%sparticipant" % newstr,
                    participants
                ))
            for i in parts:
                info += ("" if i == parts[0] else (
                    " and " if i == parts[-1] else ", "
                )) + i

            proceed = wx.OK == wx.MessageBox(
                "Copy %s\nfrom %s\ninto %s?" % (info, db1, db2),
                conf.Title, wx.OK | wx.CANCEL | wx.ICON_QUESTION
            )
            if proceed:
                self.is_merging = True
                try:
                    if not chat2:
                        chat2 = chat.copy()
                        self.chat_diff["c1" if source else "c2"] = chat2
                        chat2["id"] = db2.insert_chat(chat2, db1)
                    if (participants):
                        if contacts2:
                            db2.insert_contacts(contacts2, db1)
                        for p in participants:
                            if p in condiff:
                                condiff.remove(p)
                        db2.insert_participants(chat2, participants, db1)
                        del participants[:]
                    if (messages):
                        db2.insert_messages(chat2, messages, db1, chat)
                        del messages[:]
                finally:
                    self.is_merging = False
                for c in chats_differing:
                    if c["chat"]["c2"]["id"] == chat["id"]:
                        chats_differing.remove(c)
                db2.clear_cache()
                stc.ReadOnly = False
                stc.ClearAll()
                stc.ReadOnly = True
                event.EventObject.Enabled = False
                main.logstatus_flash("Merged %s of chat \"%s\" from %s to %s.",
                                     info, chat2["title"], db1, db2)
                # Update chat list
                db2.get_conversations_stats([chat2])
                self.chat_diff["messages%s" % (1 - source + 1)] = \
                    chat2["message_count"]
                self.chat_diff["last_message_datetime%s" % (source + 1)] = \
                    chat2["last_message_datetime"]
                if not (messages2 or participants2):
                    for i in range(self.list_chats.ItemCount):
                        chat_i = self.list_chats.GetItemMappedData(i)
                        if chat_i == self.chat_diff:
                            self.list_chats.SetItemBackgroundColour(
                                i, self.list_chats.BackgroundColour)
                self.list_chats.RefreshItems()
                stc.SetReadOnly(False), stc.ClearAll(), stc.SetReadOnly(True)
                infomsg = "Merged %s of chat \"%s\"\nfrom %s\nto %s." % \
                          (info, chat2["title"], db1, db2)
                wx.MessageBox(infomsg, conf.Title, wx.OK | wx.ICON_INFORMATION)


    def update_tabheader(self):
        """Updates page tab header with option to close page."""
        if self:
            self.ready_to_close = True
        if self:
            self.TopLevelParent.update_notebook_header()


    def load_data(self):
        """Loads data from our SkypeDatabases."""
        f1, f2 = self.db1.filename, self.db2.filename
        label_title = "Database %s vs %s:" % (
            util.htmltag("a", {"href": f1}, f1),
            util.htmltag("a", {"href": f2}, f2))
        self.html_dblabel.SetPage(label_title.decode("utf-8"))
        self.html_dblabel.BackgroundColour = self.label_all1.BackgroundColour

        try:
            # Populate the chat comparison list
            chats1 = self.db1.get_conversations()
            chats2 = self.db2.get_conversations()
            c1map = dict((c["identity"], c) for c in chats1)
            c2map = dict((c["identity"], c) for c in chats2)
            compared = []
            for c1 in chats1:
                c1["c1"], c1["c2"] = c1.copy(), c2map.get(c1["identity"])
                compared.append(c1)
            for c2 in chats2:
                if c2["identity"] not in c1map:
                    c2["c1"], c2["c2"] = None, c2.copy()
                    compared.append(c2)
            for c in compared:
                c["last_message_datetime1"] = None
                c["last_message_datetime2"] = None
                c["messages1"] = c["messages2"] = c["people"] = None
                c["diff_status"] = None

            self.list_chats.Populate(self.chats_column_map, compared)
            self.list_chats.SortListItems(3, 0) # Sort by last message in left
            self.list_chats.OnSortOrderChanged()
            self.compared = compared
            wx.CallLater(200, self.load_later_data)
        except Exception, e:
            wx.CallAfter(self.update_tabheader)
            main.logstatus_flash("Could not load chat lists from %s and %s."
                "\n\n%s", self.db1, self.db2, traceback.format_exc())
            wx.MessageBox("Could not load chat lists from %s and %s.\n\n"
                          "Error: %s." % (self.db1, self.db2, e))


    def load_later_data(self):
        """
        Loads later data from the databases, like message counts and compared
        contacts, used as a background callback to speed up page opening.
        """
        try:
            chats1 = self.db1.get_conversations()
            chats2 = self.db2.get_conversations()
            self.db1.get_conversations_stats(chats1)
            self.db2.get_conversations_stats(chats2)
            c1map = dict((c["identity"], c) for c in chats1)
            c2map = dict((c["identity"], c) for c in chats2)
            for c in self.compared:
                for i in range(2):
                    cmap = c2map if i else c1map
                    if c["c%s" % (i + 1)] and c["identity"] in cmap:
                        c["messages%s" % (i + 1)] = \
                            cmap[c["identity"]]["message_count"]
                        c["last_message_datetime%s" % (i + 1)] = \
                            cmap[c["identity"]]["last_message_datetime"]
                c["diff_status"] = self.DIFFSTATUS_DIFFERENT
                identical = False
                if c["c1"] and c["c2"]:
                    identical = (c["messages1"] == c["messages2"])
                    identical &= (c["last_message_datetime1"] \
                        == c["last_message_datetime2"]
                    )
                if identical:
                    c["diff_status"] = self.DIFFSTATUS_IDENTICAL
                people = sorted([p["identity"] for p in c["participants"]])
                if skypedata.CHATS_TYPE_SINGLE != c["type"]:
                    c["people"] = "%s (%s)" % (len(people), ", ".join(people))
                else:
                    people = [p for p in people if p != self.db1.id]
                    c["people"] = ", ".join(people)


            self.list_chats.Enabled = True
            self.list_chats.Populate(self.chats_column_map, self.compared)
            for i, c in enumerate(self.compared):
                if self.DIFFSTATUS_IDENTICAL == c["diff_status"]:
                    self.list_chats.SetItemTextColour(
                        i, conf.DiffIdenticalColour
                    )
            self.list_chats.SortListItems(3, 0) # Sort by last message in left
            self.list_chats.OnSortOrderChanged()

            # Populate the contact comparison list
            contacts1 = self.db1.get_contacts()
            contacts2 = self.db2.get_contacts()
            contactgroups1 = self.db1.get_contactgroups()
            contactgroups2 = self.db2.get_contactgroups()
            con1map = dict((c["identity"], c) for c in contacts1)
            con2map = dict((c["identity"], c) for c in contacts2)
            con1diff, con2diff = [], []
            con1new, con2new = {}, {} # New contacts for dbs {skypename:row, }
            cg1diff, cg2diff = [], []
            cg1map = dict((g["name"], g) for g in contactgroups1)
            cg2map = dict((g["name"], g) for g in contactgroups2)
            for c1 in contacts1:
                c2 = con2map.get(c1["identity"])
                if not c2 and c1["identity"] not in con1new:
                    c = c1.copy()
                    c["c1"] = c1
                    c["c2"] = c2
                    con1diff.append(c)
                    con1new[c1["identity"]] = True
            for c2 in contacts2:
                c1 = con1map.get(c2["identity"])
                if not c1 and c2["identity"] not in con2new:
                    c = c2.copy()
                    c["c1"] = c1
                    c["c2"] = c2
                    con2diff.append(c)
                    con2new[c2["identity"]] = True
            for g1 in contactgroups1:
                g2 = cg2map.get(g1["name"])
                if not g2 or g2["members"] != g1["members"]:
                    g = g1.copy()
                    g["g1"] = g1
                    g["g2"] = g2
                    cg1diff.append(g)
            for g2 in contactgroups2:
                g1 = cg1map.get(g2["name"])
                if not g1 or g1["members"] != g2["members"]:
                    g = g2.copy()
                    g["g1"] = g1
                    g["g2"] = g2
                    cg2diff.append(g)
            dummy = {"__type": "Group", "phone_mobile_normalized": "",
                "country": "", "city": "", "about": "About"}
            con1difflist = [c.copy() for c in con1diff]
            [c.update({"__type": "Contact","__data": c}) for c in con1difflist]
            for g in cg1diff:
                c = g.copy()
                c.update(dummy)
                c["identity"], c["__data"] = c["members"], g
                con1difflist.append(c)
            con2difflist = [c.copy() for c in con2diff]
            [c.update({"__type":"Contact", "__data": c}) for c in con2difflist]
            for g in cg2diff:
                c = g.copy()
                c.update(dummy)
                c["identity"], c["__data"] = c["members"], g
                con2difflist.append(c)
            self.list_contacts1.Populate(self.contacts_column_map,con1difflist)
            self.list_contacts2.Populate(self.contacts_column_map,con2difflist)
            self.button_merge_allcontacts1.Enabled = len(con1difflist) > 0
            self.button_merge_allcontacts2.Enabled = len(con2difflist) > 0
            self.con1diff = con1diff
            self.con2diff = con2diff
            self.con1difflist = con1difflist
            self.con2difflist = con2difflist
            self.congroup1diff = cg1diff
            self.congroup2diff = cg2diff

            # Some columns can be really long, pushing all else out of view
            for lst in [self.list_chats, self.list_contacts1,
                        self.list_contacts2]:
                widths = lst.GetColumnWidths()
                for i, w in [(i, w) for i, w in enumerate(widths) if w > 300]:
                    lst.SetColumnWidth(i, 300)

            for i in range(2):
                db = self.db2 if i else self.db1
                tables = db.get_tables()
                condiff = self.con2diff if i else self.con1diff
                contacts = contacts2 if i else contacts1
                db.update_fileinfo()
                label = self.label_all2 if i else self.label_all1
                label.Label = \
                    "%s.\n\nSize %s.\nLast modified %s.\n" % (
                        db, util.format_bytes(db.filesize),
                        db.last_modified.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                chats = chats2 if i else chats1
                if chats:
                    t1 = filter(None, [c["message_count"] for c in chats])
                    count_messages = sum(t1) if t1 else 0
                    t2 = filter(
                         None, [c["first_message_datetime"] for c in chats])
                    datetime_first = min(t2) if t2 else None
                    t3 = filter(
                         None, [c["last_message_datetime"] for c in chats])
                    datetime_last = max(t3) if t3 else None
                    datetext_first = "" if not datetime_first \
                        else datetime_first.strftime("%Y-%m-%d %H:%M:%S")
                    datetext_last = "" if not datetime_last \
                        else datetime_last.strftime("%Y-%m-%d %H:%M:%S")
                    contacttext = util.plural("contact", contacts)
                    if condiff:
                        contacttext += " (%d not present on the %s)" % (
                                       len(condiff), ["right", "left"][i])
                    label.Label += "%s.\n%s.\n%s.\nFirst message at %s.\n" \
                                   "Last message at %s." % (
                                   util.plural("conversation", chats),
                                   util.plural("message", count_messages), 
                                   contacttext,
                                   datetext_first, datetext_last,)
        except Exception, e:
            # Database access can easily fail if the user closes the tab before
            # the later data has been loaded.
            if self:
                main.log("Error loading additional data from %s or %s.\n\n%s",
                         self.db1, self.db2, traceback.format_exc())
                wx.MessageBox("Error loading additional data from %s or %s."
                              "\n\nError: %s." % (self.db1, self.db2, e),
                              conf.Title, wx.OK | wx.ICON_WARNING)
        if self:
            self.page_merge_all.Layout()
            self.button_swap.Enabled = True
            if self.chats_differing is None:
                self.button_scan_all.Enabled = True
            main.status_flash("Opened Skype databases %s and %s.",
                              self.db1, self.db2)
            self.Refresh()
            if "linux2" == sys.platform and wx.version().startswith("2.8"):
                wx.CallAfter(self.split_panels)
            wx.CallAfter(self.update_tabheader)



class ChatContentSTC(controls.SearchableStyledTextCtrl):
    """A StyledTextCtrl for showing and filtering chat messages."""

    def __init__(self, *args, **kwargs):
        controls.SearchableStyledTextCtrl.__init__(self, *args, **kwargs)
        self.SetUndoCollection(False)

        self._parser = None     # Current skypedata.MessageParser instance
        self._chat = None       # Currently shown chat
        self._db = None         # Database for currently shown messages
        self._page = None       # DatabasePage/MergerPage for action callbacks
        self._messages = None   # All retrieved messages (collections.deque)
        self._messages_current = None  # Currently shown (collections.deque)
        self._message_positions = {} # {msg id: (start index, end index)}
        # If set, range is centered around the message with the specified ID
        self._center_message_id =    -1
        # Index of the centered message in _messages
        self._center_message_index = -1
        self._filelinks = {} # {link end position: file path}
        self._datelinks = {} # {link end position: two dates, }
        self._datelink_last = None # Title of clicked date link, if any
        # Currently set message filter {"daterange": (datetime, datetime),
        # "text": text in message, "participants": [skypename1, ],
        # "message_id": message ID to show, range shown will be centered
        # around it}
        self._filter = {}
        self._filtertext_rgx = None # Cached regex for filter["text"]

        self._styles = {"default": 10, "bold": 11, "timestamp": 12,
            "remote": 13, "local": 14, "link": 15, "tiny": 16,
            "special": 17, "bolddefault": 18, "boldlink": 19,
            "boldspecial": 20, "remoteweak": 21, "localweak": 22,
        }
        stylespecs = {
            "default":      "face:%s,size:%d,fore:%s" %
                            (conf.HistoryFontName, conf.HistoryFontSize,
                             conf.HistoryDefaultColour),
             "bolddefault": "face:%s,size:%d,fore:%s,bold" %
                            (conf.HistoryFontName, conf.HistoryFontSize,
                             conf.HistoryDefaultColour),
             "bold":        "face:%s,size:%d,bold" %
                            (conf.HistoryFontName, conf.HistoryFontSize),
             "timestamp":   "fore:%s" % conf.HistoryTimestampColour,
             "remote":      "face:%s,size:%d,bold,fore:%s" %
                            (conf.HistoryFontName, conf.HistoryFontSize,
                             conf.HistoryRemoteAuthorColour),
             "local":       "face:%s,size:%d,bold,fore:%s" %
                            (conf.HistoryFontName, conf.HistoryFontSize,
                             conf.HistoryLocalAuthorColour),
             "remoteweak":  "face:%s,size:%d,fore:%s" %
                            (conf.HistoryFontName, conf.HistoryFontSize,
                             conf.HistoryRemoteAuthorColour),
             "localweak":   "face:%s,size:%d,fore:%s" %
                            (conf.HistoryFontName, conf.HistoryFontSize,
                             conf.HistoryLocalAuthorColour),
             "link":        "fore:%s" % conf.HistoryLinkColour,
             "boldlink":    "face:%s,bold,fore:%s" %
                            (conf.HistoryFontName, conf.HistoryLinkColour),
             "tiny":        "size:1",
             "special":     "fore:%s" % conf.HistoryGreyColour,
             "boldspecial": "face:%s,bold,fore:%s" %
                            (conf.HistoryFontName, conf.HistoryGreyColour),
        }
        self.StyleSetSpec(wx.stc.STC_STYLE_DEFAULT, stylespecs["default"])
        for style, spec in stylespecs.items():
            self.StyleSetSpec(self._styles[style], spec)
        self.StyleSetHotSpot(self._styles["link"], True)
        self.StyleSetHotSpot(self._styles["boldlink"], True)
        self.SetWrapMode(True)
        self.SetMarginLeft(10)
        self.SetReadOnly(True)
        self.Bind(wx.stc.EVT_STC_HOTSPOT_CLICK, self.OnUrl)
        # Hide caret
        self.SetCaretForeground("white")
        self.SetCaretWidth(0)


    def SetDatabasePage(self, page):
        self._page = page


    def OnUrl(self, event):
        """
        Handler for clicking a link in chat history, opens the link in system
        browser.
        """
        stc = event.EventObject
        styles_link = [self._styles["link"], self._styles["boldlink"]]
        if stc.GetStyleAt(event.Position) in styles_link:
            # Go back and forth from position and get URL range.
            url_range = {-1: -1, 1: -1} # { start and end positions }
            for step in url_range:
                pos = event.Position
                while stc.GetStyleAt(pos + step) in styles_link:
                    pos += step
                url_range[step] = pos
            url_range[1] += 1
            url = stc.GetTextRange(url_range[-1], url_range[1])
            function, params = None, []
            if url_range[-1] in self._filelinks:
                def start_file(url):
                    if os.path.exists(url):
                        util.start_file(url)
                    else:
                        messageBox(
                            "The file \"%s\" cannot be found "
                            "on this computer." % url,
                            conf.Title, wx.OK | wx.ICON_INFORMATION
                        )
                function, params = start_file, [self._filelinks[url_range[-1]]]
            elif url_range[-1] in self._datelinks:
                def filter_range(label, daterange):
                    busy = controls.BusyPanel(self._page, "Filtering messages.")
                    try:
                        self._datelink_last = label
                        self._page.chat_filter["daterange"] = daterange
                        self._page.range_date.SetValues(*daterange)
                        self.Filter = self._page.chat_filter
                        self.RefreshMessages()
                        self.ScrollToLine(0)
                        self._page.populate_chat_statistics()
                    finally:
                        busy.Close()
                function = filter_range
                params = [url, self._datelinks[url_range[-1]]]
            elif url:
                function, params = webbrowser.open, [url]
            if function:
                # Calling function here immediately will cause STC to lose
                # MouseUp, resulting in autoselect mode from click position.
                wx.CallLater(50, function, *params)
        event.StopPropagation()


    def RetrieveMessagesIfNeeded(self):
        """
        Retrieves more messages if needed, for example if current filter
        specifies a larger date range than currently available.
        """

        if not self._messages_current and "daterange" in self._filter \
        and self._filter["daterange"][0]:
            # If date filtering was just applied, check if we need to
            # retrieve more messages from earlier (messages are retrieved
            # starting from latest).
            if not self._messages[0]["datetime"] \
            or self._messages[0]["datetime"].date() \
            >= self._filter["daterange"][0]:
                m_iter = self._db.get_messages(self._chat,
                    ascending=False,
                    timestamp_from=self._messages[0]["timestamp"]
                )
                while m_iter:
                    try:
                        m = m_iter.next()
                        self._messages.appendleft(m)
                        if m["datetime"].date() < self._filter["daterange"][0]:
                            m_iter = None
                    except StopIteration, e:
                        m_iter = None
        last_dt = self._chat.get("last_message_datetime")
        if self._messages and last_dt \
        and self._messages[-1]["datetime"] < last_dt:
            # Last message timestamp is earlier than chat's last message
            # timestamp: new messages have arrived
            m_iter = self._db.get_messages(self._chat,
                ascending=True, use_cache=False,
                timestamp_from=self._messages[-1]["timestamp"]
            )
            while m_iter:
                try:
                    m = m_iter.next()
                    self._messages.append(m)
                except StopIteration, e:
                    m_iter = None


    def RefreshMessages(self, center_message_id=None):
        """
        Clears content and redisplays messages of current chat.

        @param   center_message_id  if specified, message with the ID is
                                    focused and message range will center
                                    around it, staying withing max number
        """
        self.SetReadOnly(False) # Can't modify while read-only
        self.ClearAll()
        self.SetReadOnly(True)
        self._parser = skypedata.MessageParser(self._db, self._chat, stats=True)
        if self._messages:
            self.RetrieveMessagesIfNeeded()
            self.SetReadOnly(False) # Can't modify while read-only
            self.AppendText("Formatting messages..\n")
            self.SetReadOnly(True)
            #wx.GetApp().Yield(True) # Allow UI to refresh
            self.Refresh()
            self.Freeze()
            self.SetReadOnly(False) # Can't modify while read-only
            self.ClearAll()


            if center_message_id:
                index = 0
                for m in self._messages:
                    if m["id"] == center_message_id:
                        self._center_message_id = center_message_id
                        self._center_message_index = index
                        break
                    index += 1

            colourmap = collections.defaultdict(lambda: "remote")
            colourmap[self._db.id] = "local"
            self._message_positions.clear()
            previous_day = datetime.date.fromtimestamp(0)
            count = 0
            focus_message_id = None
            self._filelinks.clear()
            self._datelinks.clear()
            # For accumulating various statistics
            rgx_highlight = re.compile(
                "(%s)" % re.escape(self._filter["text"]), re.I
            ) if ("text" in self._filter and self._filter["text"]) else None
            self._messages_current = collections.deque()

            def write_element(dom, tails_new=None):
                """
                Appends the message body to the StyledTextCtrl.

                @param   dom        xml.etree.cElementTree.Element instance
                @param   tails_new  internal use, {element: modified tail str}
                """
                tagstyle_map = {
                    "a": "link", "b": "bold", "quotefrom": "special",
                    "bodystatus": "special", "ss": "default",
                }
                to_skip = {} # {element to skip: True, }
                parent_map = dict((c, p) for p in dom.getiterator() for c in p)
                tails_new = {} if tails_new is None else tails_new
                linefeed_final = "\n\n" # Decreased if quotefrom is last

                for e in dom.getiterator():
                    # Possible tags: a|b|bodystatus|quote|quotefrom|msgstatus|
                    #                special|xml|font|blink
                    if e in to_skip:
                        continue
                    style = tagstyle_map.get(e.tag, "default")
                    text = e.text or ""
                    tail = tails_new[e] if e in tails_new else (e.tail or "")
                    children = []
                    if type(text) is str:
                        text = text.decode("utf-8")
                    if type(tail) is str:
                        tail = tail.decode("utf-8")
                    href = None
                    if "a" == e.tag:
                        href = e.get("href")
                        if href.startswith("file:"):
                            self._filelinks[self._stc.Length] = \
                                urllib.url2pathname(e.get("href")[5:])
                        linefeed_final = "\n\n"
                    elif "ss" == e.tag:
                        text = e.text
                    elif "quote" == e.tag:
                        text = "\"" + text
                        children = e.getchildren()
                        if len(children) > 1:
                            # Last element is always quotefrom
                            tails_new[children[-2]] = (children[-2].tail \
                                if children[-2].tail else "" \
                            ) + "\""
                        else:
                            text += "\""
                        linefeed_final = "\n"
                    elif "quotefrom" == e.tag:
                        text = "\n%s\n" % text
                    elif e.tag in ["xml", "b"]:
                        linefeed_final = "\n\n"
                    elif e.tag not in ["blink", "font", "bodystatus"]:
                        text = ""
                    if text:
                        self._append_text(text, style, rgx_highlight)
                    for i in children:
                        write_element(i, tails_new)
                        to_skip[i] = True
                    if tail:
                        self._append_text(tail, "default", rgx_highlight)
                        linefeed_final = "\n\n"
                if "xml" == dom.tag:
                    self._append_text(linefeed_final)

            # Assemble messages to show
            for m in self._messages:
                count += 1
                if self.IsMessageFilteredOut(m):
                    continue
                if self._center_message_index >= 0 \
                and count < self._center_message_index \
                - conf.MaxHistoryInitialMessages / 2:
                    # Skip messages before the range centered around a message
                    continue
                if self._center_message_index >= 0 \
                and count > self._center_message_index \
                + conf.MaxHistoryInitialMessages / 2:
                    # Skip messages after the range centered around a message
                    break # break for m in self._messages

                self._messages_current.append(m)

            # Add date and count information, links like "6 months"
            self._append_text("\n")
            if self._messages_current:
                m1, m2 = self._messages_current[0], self._messages_current[-1]
                self._append_text("History of  ")
                self._append_text(m1["datetime"].strftime("%d.%m.%Y"), "bold")
                if m1["datetime"].date() != m2["datetime"].date():
                    self._append_text(" to ")
                    self._append_text(
                        m2["datetime"].strftime("%d.%m.%Y"), "bold")
                self._append_text("  (%s).  " % util.plural(
                                  "message", self._messages_current))
            if self._page and self._chat["message_count"]:
                self._append_text("\nShow from:  ")
                date_first = self._chat["first_message_datetime"].date()
                date_last = self._chat["last_message_datetime"].date()
                date_until = datetime.date.today()
                dates_filter = self._filter.get("daterange")
                from_items = [] # [(title, [date_first, date_last])]
                if relativedelta:
                    for unit, count in [("day", 7), ("week", 2), ("day", 30),
                    ("month", 3), ("month", 6), ("year", 1), ("year", 2)]:
                        date_from = date_until - relativedelta(
                            **{util.plural(unit, with_items=False): count})
                        if date_from >= date_first and date_from <= date_last:
                            title = util.plural(unit, count)
                            from_items.append((title, [date_from, date_last]))
                    if date_until - relativedelta(years=2) > date_first:
                        # Warning: possible mis-showing here if chat < 4 years.
                        title = "2 to 4 years"
                        daterange = [date_until - relativedelta(years=4),
                                     date_until - relativedelta(years=2)]
                        from_items.append((title, daterange))
                    if date_until - relativedelta(years=4) > date_first:
                        title = "4 years and older"
                        daterange = [date_first,
                                     date_until - relativedelta(years=4)]
                        from_items.append((title, daterange))
                daterange = [date_first, date_last]
                from_items.append(("From the beginning", daterange))
                for i, (title, daterange) in enumerate(from_items):
                    is_active = center_message_id is None \
                                and ((title == self._datelink_last) 
                                     or (daterange == dates_filter))
                    if i:
                        self._append_text(u"  \u2022  ", "special") # bullet
                    if not is_active:
                        self._datelinks[self._stc.Length] = daterange
                    self._append_text(title, "bold" if is_active else "link")
            self._datelink_last = None
            self._append_text("\n\n")

            for i, m in enumerate(self._messages_current):
                if m["datetime"].date() != previous_day:
                    # Day has changed: insert a date header
                    previous_day = m["datetime"].date()
                    weekday, weekdate = util.get_locale_day_date(previous_day)
                    self._append_text("\n%s" % weekday, "bold")
                    self._append_text(", %s\n\n" % weekdate)

                dom = self._parser.parse(m)
                length_before = self._stc.Length
                time_value = m["datetime"].strftime("%H:%M")
                displayname = m["from_dispname"]
                special_text = "" # Special text after name, e.g. " SMS"
                body = m["body_xml"] or ""
                special_tag = dom.find("msgstatus")
                # Info messages like "/me is thirsty" -> author on same line.
                is_info = (skypedata.MESSAGES_TYPE_INFO == m["type"])

                if is_info:
                    stylebase = colourmap[m["author"]]
                    self._append_text(time_value, stylebase)
                    self._append_text("\n%s " % displayname, stylebase + "weak")
                elif special_tag is None:
                    self._append_text("%s %s\n" % (time_value, displayname),
                                                   colourmap[m["author"]])
                else:
                    self._append_text("%s %s" % (time_value, displayname),
                                                 colourmap[m["author"]])
                    self._append_text("%s\n" % special_tag.text, "special")

                write_element(dom)

                # Store message position for FocusMessage()
                length_after = self._stc.Length
                self._message_positions[m["id"]] = (
                    length_before, length_after - 2
                )
                if self._center_message_id == m["id"]:
                    focus_message_id = m["id"]
                if i and not i % conf.MaxHistoryInitialMessages:
                    wx.Yield() # For responsive GUI while showing many messages

            # Reset the centered message data, as filtering should override it
            self._center_message_index = -1
            self._center_message_id = -1
            self.SetReadOnly(True)
            if focus_message_id:
                self.FocusMessage(focus_message_id)
            else:
                self.ScrollToLine(self.LineCount)
            self.Thaw()
        else:
            # No messages to show
            self.SetReadOnly(False) # Can't modify while read-only
            self.ClearAll()
            self._append_text("\nNo messages to show.", "special")
            self.SetReadOnly(True)


    def _append_text(self, text, style="default", rgx_highlight=None):
        """
        Appends text to the StyledTextCtrl in the specified style.

        @param   rgx_highlight  if set, substrings matching the regex are added
                                in highlighted style
        """
        text = text or ""
        if type(text) is unicode:
            text = text.encode("utf-8")
        text_parts = rgx_highlight.split(text) if rgx_highlight else [text]
        stc = self._stc
        bold = "bold%s" % style if "bold%s" % style in self._styles else style
        len_self = self.GetTextLength()
        stc.AppendTextUTF8(text)
        stc.StartStyling(pos=len_self, mask=0xFF)
        stc.SetStyling(length=len(text), style=self._styles[style])
        for i, t in enumerate(text_parts):
            if i % 2:
                stc.StartStyling(pos=len_self, mask=0xFF)
                stc.SetStyling(length=len(t), style=self._styles[bold])
            len_self += len(t)


    def _append_multiline(self, text, indent):
        """
        Appends text with new lines indented at the specified level.
        """
        if "\n" in text:
            for line in text.split("\n"):
                self._append_text("%s\n" % line)
                if self.USE_COLUMNS:
                    self.SetLineIndentation(self.LineCount - 1, indent)
            if self.USE_COLUMNS:
                self.SetLineIndentation(self.LineCount - 1, 0)
        else:
            self._append_text(text)


    def Populate(self, chat, db, messages=None, center_message_id=None):
        """
        Populates the chat history with messages from the specified chat.

        @param   chat               chat data, as returned from SkypeDatabase
        @param   db                 SkypeDatabase to use
        @param   messages           messages to show (if set, messages are not
                                    retrieved from database)
        @param   center_message_id  if set, specifies the message around which
                                    to center other messages in the shown range
        """
        message_show_limit = conf.MaxHistoryInitialMessages
        if messages:
            message_show_limit = len(messages)

        self.ClearAll()
        self.Refresh()
        self._center_message_index = -1
        self._center_message_id = -1

        if messages is not None:
            message_range = collections.deque(messages)
        else:
            m_iter = db.get_messages(chat, ascending=False)

            i = 0
            message_range = collections.deque()
            try:
                iterate = (i < message_show_limit)
                while iterate:
                    m = m_iter.next()
                    if m:
                        i += 1
                        message_range.appendleft(m)
                        if m["id"] == center_message_id:
                            self._center_message_index = len(message_range)
                            self._center_message_id = center_message_id
                    else:
                        break # break while iterate
                    if center_message_id:
                        iterate = (self._center_message_index < 0) or (
                            len(message_range) < self._center_message_index \
                                + message_show_limit / 2
                        )
                    else:
                        iterate = (i < message_show_limit)
            except StopIteration:
                m_iter = None
            if self._center_message_index >= 0:
                self._center_message_index = \
                    len(message_range) - self._center_message_index

        self._chat = chat
        self._db = db
        self._messages_current = message_range
        self._messages = copy.copy(message_range)
        self._calls = db.get_calls(chat)
        self._filter["daterange"] = [
            message_range[0]["datetime"].date() if message_range else None,
            message_range[-1]["datetime"].date() if message_range else None
        ]
        self.RefreshMessages()


    def FocusMessage(self, message_id):
        """Selects and scrolls the specified message into view."""
        if message_id in self._message_positions:
            padding = -50 # So that selection does not finish at visible edge
            for p in self._message_positions[message_id]:
                # Ensure that both ends of the selection are visible
                self._stc.CurrentPos = p + padding
                self.EnsureCaretVisible()
                padding = abs(padding)
            self._stc.SetSelection(*self._message_positions[message_id])


    def IsMessageFilteredOut(self, message):
        """
        Returns whether the specified message does not pass the current filter.
        """
        result = False
        if "participants" in self._filter \
        and self._filter["participants"] \
        and message["author"] not in self._filter["participants"] \
        and message["author"] \
        in [p["identity"] for p in self._chat["participants"]]:
            # Last check among chat participants is for cases where contact is
            # not listed among chat participants at all (e.g. left at once)
            result = True
        elif "daterange" in self._filter \
        and not (self._filter["daterange"][0] <= message["datetime"].date() \
        <= self._filter["daterange"][1]):
            result = True
        elif "text" in self._filter and self._filter["text"]:
            if not self._filtertext_rgx:
                self._filtertext_rgx = re.compile(re.escape(
                    self._filter["text"]
                ), re.IGNORECASE)
            if not message["body_xml"] \
            or not self._filtertext_rgx.search(message["body_xml"]):
                result = True
        return result


    def IsMessageShown(self, message_id):
        """Returns whether the specified message is currently shown."""
        return (message_id in self._message_positions)


    def GetMessage(self, index):
        """
        Returns the message at the specified index in the currently shown
        messages.

        @param   index  list index (negative starts from end)
        """
        if self._messages_current and index < 0:
            index += len(self._messages_current)
        m = self._messages_current[index] if self._messages_current \
                and 0 <= index < len(self._messages_current) \
            else None
        return m


    def GetMessages(self):
        """Returns a list of all the currently shown messages."""
        result = []
        if self._messages_current:
            result = list(self._messages_current)
        return result


    def GetRetrievedMessages(self):
        """Returns a list of all retrieved messages."""
        result = []
        if self._messages:
            result = list(self._messages)
        return result


    def SetFilter(self, filter_data):
        """
        Sets the filter to use for the current chat. Does not refresh messages.

        @param   filter_data  None or {"daterange":
                              (datetime, datetime), "text": text in message,
                              "participants": [skypename1, ]}
        """
        filter_data = filter_data or {}
        if not util.cmp_dicts(self._filter, filter_data):
            self._filter = copy.deepcopy(filter_data)
            self._filtertext_rgx = None
            self._messages_current = None
    def GetFilter(self):
        return copy.deepcopy(self._filter)
    Filter = property(GetFilter, SetFilter, doc=\
        """
        The filter to use for the current chat. {"daterange":
        (datetime, datetime), "text": text in message,
        "participants": [skypename1, ]}
        """
    )


    def GetStatisticsHtml(self, sort_field="name"):
        """
        Returns the statistics collected during last Populate as HTML, or "".
        """
        result = ""
        stats = self._parser and self._parser.get_collected_stats()
        if stats:
            participants = [p["contact"] for p in self._chat["participants"]]
            data = {"db": self._db, "participants": participants,
                    "sort_by": sort_field, "stats": stats, }
            result = step.Template(templates.STATS_HTML).expand(data)
        return result



class DayHourDialog(wx.Dialog):
    """Popup dialog for entering two values, days and hours."""

    def __init__(self, parent, message, caption, days, hours):
        wx.Dialog.__init__(self, parent=parent, title=caption, size=(250, 200))

        vbox = self.Sizer = wx.BoxSizer(wx.VERTICAL)

        self.text_days = wx.SpinCtrl(parent=self, style=wx.ALIGN_LEFT,
            size=(200, -1), value=str(days), min=-sys.maxsize, max=sys.maxsize
        )
        hbox1 = wx.BoxSizer(wx.HORIZONTAL)
        hbox1.AddStretchSpacer()
        hbox1.Add(wx.StaticText(parent=self, label="Days:"),
            flag=wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        hbox1.Add(self.text_days, border=5, flag=wx.LEFT | wx.ALIGN_RIGHT)

        self.text_hours = wx.SpinCtrl(parent=self, style=wx.ALIGN_LEFT,
           size=(200, -1), value=str(hours), min=-sys.maxsize, max=sys.maxsize)
        hbox2 = wx.BoxSizer(wx.HORIZONTAL)
        hbox2.AddStretchSpacer()
        hbox2.Add(wx.StaticText(parent=self, label="Hours:"),
                  flag=wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        hbox2.Add(self.text_hours, border=5, flag=wx.LEFT | wx.ALIGN_RIGHT)

        button_ok = wx.Button(self, label="OK")
        button_cancel = wx.Button(self, label="Cancel", id=wx.ID_CANCEL)
        hbox3 = wx.BoxSizer(wx.HORIZONTAL)
        hbox3.AddStretchSpacer()
        hbox3.Add(button_ok, border=5, flag=wx.RIGHT)
        hbox3.Add(button_cancel, border=5, flag=wx.RIGHT)

        vbox.Add(
            wx.StaticText(parent=self, label=message), border=10, flag=wx.ALL)
        vbox.AddSpacer(5)
        vbox.Add(
            hbox1, border=5, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND)
        vbox.Add(
            hbox2, border=5, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND)
        vbox.Add(wx.StaticLine(self), border=5, proportion=1,
                 flag=wx.LEFT | wx.RIGHT | wx.EXPAND)
        vbox.Add(hbox3, border=5, flag=wx.ALL | wx.EXPAND)

        button_ok.SetDefault()
        button_ok.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_OK))
        button_cancel.Bind(wx.EVT_BUTTON, lambda e:self.EndModal(wx.ID_CANCEL))

        self.Layout()
        self.Size = self.GetEffectiveMinSize()
        self.CenterOnParent()


    def GetValues(self):
        """Returns the entered days and hours as a tuple of integers."""
        days = self.text_days.Value
        hours = self.text_hours.Value
        return days, hours



class SkypeHandler(Skype4Py.Skype if Skype4Py else object):
    """A convenience wrapper around Skype4Py functionality."""

    def shutdown(self):
        """Posts a message to the running Skype application to close itself."""
        self.Client.Shutdown()


    def is_running(self):
        """Returns whether Skype is currently running."""
        return self.Client.IsRunning


    def launch(self):
        """Tries to launch Skype."""
        self.Client.Start()


    def search_users(self, value):
        """
        Searches for users with the specified value (either name, phone or
        e-mail) in the currently running Skype application.
        """
        if not self.is_running():
            self.launch()
        self.FriendlyName = conf.Title
        self.Attach() # Should open a confirmation dialog in Skype

        result = list(self.SearchForUsers(value))
        return result


    def add_to_contacts(self, users):
        """
        Adds the specified Skype4Py.User instances to Skype contacts in the
        currently running Skype application.
        """
        if not self.is_running():
            self.launch()
        self.FriendlyName = conf.Title
        self.Attach() # Should open a confirmation dialog in Skype
        for user in users:
            user.BuddyStatus = Skype4Py.enums.budPendingAuthorization
        self.Client.Focus()



class AboutDialog(wx.Dialog):
 
    def __init__(self, parent, content):
        wx.Dialog.__init__(self, parent, title="About %s" % conf.Title,
                           style=wx.CAPTION | wx.CLOSE_BOX)
        html = self.html = wx.html.HtmlWindow(self)
        html.SetPage(content)
        html.BackgroundColour = self.BackgroundColour
        html.Bind(wx.html.EVT_HTML_LINK_CLICKED,
                  lambda e: webbrowser.open(e.GetLinkInfo().Href))
        sizer_buttons = self.CreateButtonSizer(wx.OK)

        self.Sizer = wx.BoxSizer(wx.VERTICAL)
        self.Sizer.Add(html, proportion=1, flag=wx.GROW)
        self.Sizer.Add(sizer_buttons, border=8,
                       flag=wx.ALIGN_CENTER | wx.ALL)
        self.Layout()
        self.Size = (self.Size[0], html.VirtualSize[1] + 50)
        self.CenterOnParent()



def messageBox(message, title, style):
    """
    Shows a non-native message box, with no bell sound for any style, returning
    the message box result code."""
    dlg = wx.lib.agw.genericmessagedialog.GenericMessageDialog(
        None, message, title, style
    )
    result = dlg.ShowModal()
    dlg.Destroy()
    return result
