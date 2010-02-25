#
# Copyright (C) 2007 Red Hat, Inc.
# Copyright (C) 2007 Daniel P. Berrange <berrange@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.
#

import gobject
import gtk
import gtk.glade
import traceback
import logging

from virtinst import Storage
from virtinst import Interface

from virtManager import uihelpers
from virtManager import util
from virtManager.connection import vmmConnection
from virtManager.createnet import vmmCreateNetwork
from virtManager.createpool import vmmCreatePool
from virtManager.createvol import vmmCreateVolume
from virtManager.createinterface import vmmCreateInterface
from virtManager.error import vmmErrorDialog
from virtManager.graphwidgets import Sparkline

INTERFACE_PAGE_INFO = 0
INTERFACE_PAGE_ERROR = 1

class vmmHost(gobject.GObject):
    __gsignals__ = {
        "action-show-help": (gobject.SIGNAL_RUN_FIRST,
                               gobject.TYPE_NONE, [str]),
        "action-exit-app": (gobject.SIGNAL_RUN_FIRST,
                            gobject.TYPE_NONE, []),
        "action-view-manager": (gobject.SIGNAL_RUN_FIRST,
                                gobject.TYPE_NONE, []),
        }
    def __init__(self, config, conn, engine):
        self.__gobject_init__()
        self.window = gtk.glade.XML(config.get_glade_dir() + "/vmm-host.glade",
                                    "vmm-host", domain="virt-manager")
        self.config = config
        self.conn = conn
        self.engine = engine

        self.topwin = self.window.get_widget("vmm-host")
        self.topwin.hide()

        self.err = vmmErrorDialog(self.topwin,
                                  0, gtk.MESSAGE_ERROR, gtk.BUTTONS_CLOSE,
                                  _("Unexpected Error"),
                                  _("An unexpected error occurred"))

        self.title = conn.get_short_hostname() + " " + self.topwin.get_title()
        self.topwin.set_title(self.title)

        self.PIXBUF_STATE_RUNNING = gtk.gdk.pixbuf_new_from_file_at_size(self.config.get_icon_dir() + "/state_running.png", 18, 18)
        self.PIXBUF_STATE_SHUTOFF = gtk.gdk.pixbuf_new_from_file_at_size(self.config.get_icon_dir() + "/state_shutoff.png", 18, 18)

        self.addnet = None
        self.addpool = None
        self.addvol = None
        self.addinterface = None
        self.volmenu = None

        self.cpu_usage_graph = None
        self.memory_usage_graph = None
        self.init_conn_state()

        # Set up signals
        self.window.get_widget("net-list").get_selection().connect("changed", self.net_selected)
        self.window.get_widget("vol-list").get_selection().connect("changed", self.vol_selected)
        self.window.get_widget("interface-list").get_selection().connect("changed", self.interface_selected)


        self.init_net_state()
        self.init_storage_state()
        self.init_interface_state()

        self.conn.connect("net-added", self.repopulate_networks)
        self.conn.connect("net-removed", self.repopulate_networks)
        self.conn.connect("net-started", self.refresh_network)
        self.conn.connect("net-stopped", self.refresh_network)

        self.conn.connect("pool-added", self.repopulate_storage_pools)
        self.conn.connect("pool-removed", self.repopulate_storage_pools)
        self.conn.connect("pool-started", self.refresh_storage_pool)
        self.conn.connect("pool-stopped", self.refresh_storage_pool)

        self.conn.connect("interface-added", self.repopulate_interfaces)
        self.conn.connect("interface-removed", self.repopulate_interfaces)
        self.conn.connect("interface-started", self.refresh_interface)
        self.conn.connect("interface-stopped", self.refresh_interface)

        self.conn.connect("state-changed", self.conn_state_changed)

        self.window.signal_autoconnect({
            "on_menu_file_view_manager_activate" : self.view_manager,
            "on_menu_file_quit_activate" : self.exit_app,
            "on_menu_file_close_activate": self.close,
            "on_vmm_host_delete_event": self.close,

            "on_menu_help_contents_activate": self.show_help,

            "on_net_add_clicked": self.add_network,
            "on_net_delete_clicked": self.delete_network,
            "on_net_stop_clicked": self.stop_network,
            "on_net_start_clicked": self.start_network,
            "on_net_autostart_toggled": self.net_autostart_changed,
            "on_net_apply_clicked": self.net_apply,

            "on_pool_add_clicked" : self.add_pool,
            "on_vol_add_clicked" : self.add_vol,
            "on_pool_stop_clicked": self.stop_pool,
            "on_pool_start_clicked": self.start_pool,
            "on_pool_delete_clicked": self.delete_pool,
            "on_pool_autostart_toggled": self.pool_autostart_changed,
            "on_vol_delete_clicked": self.delete_vol,
            "on_vol_list_button_press_event": self.popup_vol_menu,
            "on_pool_apply_clicked": self.pool_apply,

            "on_interface_add_clicked" : self.add_interface,
            "on_interface_start_clicked" : self.start_interface,
            "on_interface_stop_clicked" : self.stop_interface,
            "on_interface_delete_clicked" : self.delete_interface,
            "on_interface_startmode_changed": self.interface_startmode_changed,
            "on_interface_apply_clicked" : self.interface_apply,

            "on_config_autoconnect_toggled": self.toggle_autoconnect,
            })

        # XXX: Help docs useless/out of date
        self.window.get_widget("help_menuitem").hide()
        finish_img = gtk.image_new_from_stock(gtk.STOCK_DELETE,
                                              gtk.ICON_SIZE_BUTTON)
        self.window.get_widget("vol-delete").set_image(finish_img)
        finish_img = gtk.image_new_from_stock(gtk.STOCK_NEW,
                                              gtk.ICON_SIZE_BUTTON)
        self.window.get_widget("vol-add").set_image(finish_img)

        self.conn.connect("resources-sampled", self.refresh_resources)
        self.reset_state()


    def init_net_state(self):
        self.window.get_widget("network-pages").set_show_tabs(False)

        # [ unique, label, icon name, icon size, is_active ]
        netListModel = gtk.ListStore(str, str, str, int, bool)
        self.window.get_widget("net-list").set_model(netListModel)

        netCol = gtk.TreeViewColumn("Networks")
        netCol.set_spacing(6)
        net_txt = gtk.CellRendererText()
        net_img = gtk.CellRendererPixbuf()
        netCol.pack_start(net_img, False)
        netCol.pack_start(net_txt, True)
        netCol.add_attribute(net_txt, 'text', 1)
        netCol.add_attribute(net_txt, 'sensitive', 4)
        netCol.add_attribute(net_img, 'icon-name', 2)
        netCol.add_attribute(net_img, 'stock-size', 3)
        self.window.get_widget("net-list").append_column(netCol)
        netListModel.set_sort_column_id(1, gtk.SORT_ASCENDING)

        self.populate_networks(netListModel)

        if not self.conn.network_capable:
            self.set_net_error_page(
                _("Libvirt connection does not support virtual network "
                  "management."))

    def init_storage_state(self):
        self.window.get_widget("storage-pages").set_show_tabs(False)

        self.volmenu = gtk.Menu()
        volCopyPath = gtk.ImageMenuItem(_("Copy Volume Path"))
        volCopyImage = gtk.Image()
        volCopyImage.set_from_stock(gtk.STOCK_COPY, gtk.ICON_SIZE_MENU)
        volCopyPath.set_image(volCopyImage)
        volCopyPath.show()
        volCopyPath.connect("activate", self.copy_vol_path)
        self.volmenu.add(volCopyPath)

        volListModel = gtk.ListStore(str, str, str, str)
        self.window.get_widget("vol-list").set_model(volListModel)

        volCol = gtk.TreeViewColumn("Volumes")
        vol_txt1 = gtk.CellRendererText()
        volCol.pack_start(vol_txt1, True)
        volCol.add_attribute(vol_txt1, 'text', 1)
        volCol.set_sort_column_id(1)
        self.window.get_widget("vol-list").append_column(volCol)

        volSizeCol = gtk.TreeViewColumn("Size")
        vol_txt2 = gtk.CellRendererText()
        volSizeCol.pack_start(vol_txt2, False)
        volSizeCol.add_attribute(vol_txt2, 'text', 2)
        volSizeCol.set_sort_column_id(2)
        self.window.get_widget("vol-list").append_column(volSizeCol)

        volFormatCol = gtk.TreeViewColumn("Format")
        vol_txt3 = gtk.CellRendererText()
        volFormatCol.pack_start(vol_txt3, False)
        volFormatCol.add_attribute(vol_txt3, 'text', 3)
        volFormatCol.set_sort_column_id(3)
        self.window.get_widget("vol-list").append_column(volFormatCol)

        volListModel.set_sort_column_id(1, gtk.SORT_ASCENDING)

        init_pool_list(self.window.get_widget("pool-list"),
                       self.pool_selected)
        populate_storage_pools(self.window.get_widget("pool-list"),
                               self.conn)

        if not self.conn.storage_capable:
            self.set_storage_error_page(
                _("Libvirt connection does not support storage management."))

    def init_interface_state(self):
        self.window.get_widget("interface-pages").set_show_tabs(False)

        # [ unique, label, icon name, icon size, is_active ]
        interfaceListModel = gtk.ListStore(str, str, str, int, bool)
        self.window.get_widget("interface-list").set_model(interfaceListModel)

        interfaceCol = gtk.TreeViewColumn("Interfaces")
        interfaceCol.set_spacing(6)
        interface_txt = gtk.CellRendererText()
        interface_img = gtk.CellRendererPixbuf()
        interfaceCol.pack_start(interface_img, False)
        interfaceCol.pack_start(interface_txt, True)
        interfaceCol.add_attribute(interface_txt, 'text', 1)
        interfaceCol.add_attribute(interface_txt, 'sensitive', 4)
        interfaceCol.add_attribute(interface_img, 'icon-name', 2)
        interfaceCol.add_attribute(interface_img, 'stock-size', 3)
        self.window.get_widget("interface-list").append_column(interfaceCol)
        interfaceListModel.set_sort_column_id(1, gtk.SORT_ASCENDING)

        # Starmode combo
        uihelpers.build_startmode_combo(
            self.window.get_widget("interface-startmode"))

        # [ name, type ]
        childListModel = gtk.ListStore(str, str)
        childList = self.window.get_widget("interface-child-list")
        childList.set_model(childListModel)

        childNameCol = gtk.TreeViewColumn("Name")
        child_txt1 = gtk.CellRendererText()
        childNameCol.pack_start(child_txt1, True)
        childNameCol.add_attribute(child_txt1, 'text', 0)
        childNameCol.set_sort_column_id(0)
        childList.append_column(childNameCol)

        childTypeCol = gtk.TreeViewColumn("Interface Type")
        child_txt2 = gtk.CellRendererText()
        childTypeCol.pack_start(child_txt2, True)
        childTypeCol.add_attribute(child_txt2, 'text', 1)
        childTypeCol.set_sort_column_id(1)
        childList.append_column(childTypeCol)
        childListModel.set_sort_column_id(0, gtk.SORT_ASCENDING)

        self.populate_interfaces(interfaceListModel)

        if not self.conn.interface_capable:
            self.set_interface_error_page(
                _("Libvirt connection does not support interface management."))

    def init_conn_state(self):
        uri = self.conn.get_uri()
        host = self.conn.get_hostname()
        drv = self.conn.get_driver()
        memory = self.conn.pretty_host_memory_size()
        proc = self.conn.host_active_processor_count()
        arch = self.conn.host_architecture()
        auto = self.conn.get_autoconnect()

        self.window.get_widget("overview-uri").set_text(uri)
        self.window.get_widget("overview-hostname").set_text(host)
        self.window.get_widget("overview-hypervisor").set_text(drv)
        self.window.get_widget("overview-memory").set_text(memory)
        self.window.get_widget("overview-cpus").set_text(str(proc))
        self.window.get_widget("overview-arch").set_text(arch)
        self.window.get_widget("config-autoconnect").set_active(auto)

        self.cpu_usage_graph = Sparkline()
        self.cpu_usage_graph.show()
        self.window.get_widget("performance-table").attach(self.cpu_usage_graph,                                                           1, 2, 0, 1)

        self.memory_usage_graph = Sparkline()
        self.memory_usage_graph.show()
        self.window.get_widget("performance-table").attach(self.memory_usage_graph,
                                                           1, 2, 1, 2)


    def show(self):
        if self.is_visible():
            self.topwin.present()
            return
        self.topwin.present()

        self.engine.increment_window_counter()

    def is_visible(self):
        if self.window.get_widget("vmm-host").flags() & gtk.VISIBLE:
            return 1
        return 0

    def close(self,ignore1=None,ignore2=None):
        if self.is_visible():
            self.window.get_widget("vmm-host").hide()
            self.engine.decrement_window_counter()
        return 1

    def show_help(self, src):
        self.emit("action-show-help", "virt-manager-host-window")

    def view_manager(self, src):
        self.emit("action-view-manager")

    def exit_app(self, src):
        self.emit("action-exit-app")

    def reset_state(self):
        self.refresh_resources()
        self.conn_state_changed()

        # Update autostart value
        auto = self.conn.get_autoconnect()
        self.window.get_widget("config-autoconnect").set_active(auto)

    def refresh_resources(self, ignore=None):
        self.window.get_widget("performance-cpu").set_text("%d %%" % self.conn.cpu_time_percentage())
        vm_memory = self.conn.pretty_current_memory()
        host_memory = self.conn.pretty_host_memory_size()
        self.window.get_widget("performance-memory").set_text(_("%(currentmem)s of %(maxmem)s") % {'currentmem': vm_memory, 'maxmem': host_memory})

        cpu_vector = self.conn.cpu_time_vector()
        cpu_vector.reverse()
        self.cpu_usage_graph.set_property("data_array", cpu_vector)

        memory_vector = self.conn.current_memory_vector()
        memory_vector.reverse()
        self.memory_usage_graph.set_property("data_array", memory_vector)

    def conn_state_changed(self, ignore1=None):
        state = (self.conn.get_state() == vmmConnection.STATE_ACTIVE)
        self.window.get_widget("net-add").set_sensitive(state)
        self.window.get_widget("pool-add").set_sensitive(state)

    def toggle_autoconnect(self, src):
        self.conn.set_autoconnect(src.get_active())

    # -------------------------
    # Virtual Network functions
    # -------------------------

    def delete_network(self, src):
        net = self.current_network()
        if net is None:
            return

        result = self.err.yes_no(_("Are you sure you want to permanently "
                                   "delete the network %s?") % net.get_name())
        if not result:
            return
        try:
            net.delete()
        except Exception, e:
            self.err.show_err(_("Error deleting network: %s") % str(e),
                              "".join(traceback.format_exc()))

    def start_network(self, src):
        net = self.current_network()
        if net is None:
            return

        try:
            net.start()
        except Exception, e:
            self.err.show_err(_("Error starting network: %s") % str(e),
                              "".join(traceback.format_exc()))

    def stop_network(self, src):
        net = self.current_network()
        if net is None:
            return

        try:
            net.stop()
        except Exception, e:
            self.err.show_err(_("Error stopping network: %s") % str(e),
                              "".join(traceback.format_exc()))

    def add_network(self, src):
        try:
            if self.addnet is None:
                self.addnet = vmmCreateNetwork(self.config, self.conn)
            self.addnet.show()
        except Exception, e:
            self.err.show_err(_("Error launching network wizard: %s") % str(e),
                              "".join(traceback.format_exc()))

    def net_apply(self, src):
        net = self.current_network()
        if net is None:
            return

        try:
            net.set_autostart(self.window.get_widget("net-autostart").get_active())
        except Exception, e:
            self.err.show_err(_("Error setting net autostart: %s") % str(e),
                              "".join(traceback.format_exc()))
            return
        self.window.get_widget("net-apply").set_sensitive(False)

    def net_autostart_changed(self, src):
        auto = self.window.get_widget("net-autostart").get_active()
        self.window.get_widget("net-autostart").set_label(auto and \
                                                          _("On Boot") or \
                                                          _("Never"))
        self.window.get_widget("net-apply").set_sensitive(True)

    def current_network(self):
        sel = self.window.get_widget("net-list").get_selection()
        active = sel.get_selected()
        if active[1] != None:
            curruuid = active[0].get_value(active[1], 0)
            return self.conn.get_net(curruuid)
        return None

    def refresh_network(self, src, uri, uuid):
        sel = self.window.get_widget("net-list").get_selection()
        active = sel.get_selected()
        if active[1] != None:
            curruuid = active[0].get_value(active[1], 0)
            if curruuid == uuid:
                self.net_selected(sel)

    def set_net_error_page(self, msg):
        self.reset_net_state()
        self.window.get_widget("network-pages").set_current_page(1)
        self.window.get_widget("network-error-label").set_text(msg)

    def net_selected(self, src):
        selected = src.get_selected()
        if selected[1] == None or \
           selected[0].get_value(selected[1], 0) == None:
            self.set_net_error_page(_("No virtual network selected."))
            return

        self.window.get_widget("network-pages").set_current_page(0)
        self.window.get_widget("net-apply").set_sensitive(False)
        net = self.conn.get_net(selected[0].get_value(selected[1], 0))

        try:
            self.populate_net_state(net)
        except Exception, e:
            logging.exception(e)
            self.set_net_error_page(_("Error selecting network: %s") % e)

    def populate_net_state(self, net):
        active = net.is_active()

        self.window.get_widget("net-details").set_sensitive(True)
        self.window.get_widget("net-name").set_text(net.get_name())

        dev = active and net.get_bridge_device() or ""
        state = active and _("Active") or _("Inactive")
        icon = (active and self.PIXBUF_STATE_RUNNING or
                           self.PIXBUF_STATE_SHUTOFF)

        self.window.get_widget("net-device").set_text(dev)
        self.window.get_widget("net-device").set_sensitive(active)
        self.window.get_widget("net-state").set_text(state)
        self.window.get_widget("net-state-icon").set_from_pixbuf(icon)

        self.window.get_widget("net-start").set_sensitive(not active)
        self.window.get_widget("net-stop").set_sensitive(active)
        self.window.get_widget("net-delete").set_sensitive(not active)

        autostart = net.get_autostart()
        autolabel = autostart and _("On Boot") or _("Never")
        self.window.get_widget("net-autostart").set_active(autostart)
        self.window.get_widget("net-autostart").set_label(autolabel)

        network = net.get_ipv4_network()
        self.window.get_widget("net-ip4-network").set_text(str(network))

        dhcp = net.get_ipv4_dhcp_range()
        start = dhcp and str(dhcp[0]) or _("Disabled")
        end = dhcp and str(dhcp[1]) or _("Disabled")
        self.window.get_widget("net-ip4-dhcp-start").set_text(start)
        self.window.get_widget("net-ip4-dhcp-end").set_text(end)

        forward, ignore = net.get_ipv4_forward()
        iconsize = gtk.ICON_SIZE_MENU
        icon = forward and gtk.STOCK_CONNECT or gtk.STOCK_DISCONNECT

        self.window.get_widget("net-ip4-forwarding-icon").set_from_stock(
                                                        icon, iconsize)

        forward_str = net.pretty_forward_mode()
        self.window.get_widget("net-ip4-forwarding").set_text(forward_str)


    def reset_net_state(self):
        self.window.get_widget("net-details").set_sensitive(False)
        self.window.get_widget("net-name").set_text("")
        self.window.get_widget("net-device").set_text("")
        self.window.get_widget("net-device").set_sensitive(False)
        self.window.get_widget("net-state").set_text(_("Inactive"))
        self.window.get_widget("net-state-icon").set_from_pixbuf(self.PIXBUF_STATE_SHUTOFF)
        self.window.get_widget("net-start").set_sensitive(False)
        self.window.get_widget("net-stop").set_sensitive(False)
        self.window.get_widget("net-delete").set_sensitive(False)
        self.window.get_widget("net-autostart").set_label(_("Never"))
        self.window.get_widget("net-autostart").set_active(False)
        self.window.get_widget("net-ip4-network").set_text("")
        self.window.get_widget("net-ip4-dhcp-start").set_text("")
        self.window.get_widget("net-ip4-dhcp-end").set_text("")
        self.window.get_widget("net-ip4-forwarding-icon").set_from_stock(gtk.STOCK_DISCONNECT, gtk.ICON_SIZE_MENU)
        self.window.get_widget("net-ip4-forwarding").set_text(_("Isolated virtual network"))
        self.window.get_widget("net-apply").set_sensitive(False)

    def repopulate_networks(self, src, uri, uuid):
        self.populate_networks(self.window.get_widget("net-list").get_model())

    def populate_networks(self, model):
        net_list = self.window.get_widget("net-list")
        model.clear()
        for uuid in self.conn.list_net_uuids():
            net = self.conn.get_net(uuid)
            model.append([uuid, net.get_name(), "network-idle",
                          gtk.ICON_SIZE_LARGE_TOOLBAR,
                          bool(net.is_active())])

        _iter = model.get_iter_first()
        if _iter:
            net_list.get_selection().select_iter(_iter)
        net_list.get_selection().emit("changed")


    # ------------------------------
    # Storage Manager methods
    # ------------------------------


    def stop_pool(self, src):
        pool = self.current_pool()
        if pool is not None:
            try:
                pool.stop()
            except Exception, e:
                self.err.show_err(_("Error starting pool '%s': %s") % \
                                    (pool.get_name(), str(e)),
                                  "".join(traceback.format_exc()))

    def start_pool(self, src):
        pool = self.current_pool()
        if pool is not None:
            try:
                pool.start()
            except Exception, e:
                self.err.show_err(_("Error starting pool '%s': %s") % \
                                    (pool.get_name(), str(e)),
                                  "".join(traceback.format_exc()))

    def delete_pool(self, src):
        pool = self.current_pool()
        if pool is None:
            return

        result = self.err.yes_no(_("Are you sure you want to permanently "
                                   "delete the pool %s?") % pool.get_name())
        if not result:
            return
        try:
            pool.delete()
        except Exception, e:
            self.err.show_err(_("Error deleting pool: %s") % str(e),
                              "".join(traceback.format_exc()))

    def delete_vol(self, src):
        vol = self.current_vol()
        if vol is None:
            return

        result = self.err.yes_no(_("Are you sure you want to permanently "
                                   "delete the volume %s?") % vol.get_name())
        if not result:
            return

        try:
            vol.delete()
            self.refresh_current_pool()
        except Exception, e:
            self.err.show_err(_("Error deleting volume: %s") % str(e),
                              "".join(traceback.format_exc()))
            return
        self.populate_storage_volumes()

    def add_pool(self, src):
        try:
            if self.addpool is None:
                self.addpool = vmmCreatePool(self.config, self.conn)
            self.addpool.show()
        except Exception, e:
            self.err.show_err(_("Error launching pool wizard: %s") % str(e),
                              "".join(traceback.format_exc()))

    def add_vol(self, src):
        pool = self.current_pool()
        if pool is None:
            return
        try:
            if self.addvol is None:
                self.addvol = vmmCreateVolume(self.config, self.conn, pool)
                self.addvol.connect("vol-created", self.refresh_current_pool)
            else:
                self.addvol.set_parent_pool(pool)
            self.addvol.show()
        except Exception, e:
            self.err.show_err(_("Error launching volume wizard: %s") % str(e),
                              "".join(traceback.format_exc()))

    def refresh_current_pool(self, ignore1=None):
        cp = self.current_pool()
        if cp is None:
            return
        cp.refresh()
        self.refresh_storage_pool(None, None, cp.get_uuid())

    def current_pool(self):
        sel = self.window.get_widget("pool-list").get_selection()
        active = sel.get_selected()
        if active[1] != None:
            curruuid = active[0].get_value(active[1], 0)
            return self.conn.get_pool(curruuid)
        return None

    def current_vol(self):
        pool = self.current_pool()
        if not pool:
            return None
        sel = self.window.get_widget("vol-list").get_selection()
        active = sel.get_selected()
        if active[1] != None:
            curruuid = active[0].get_value(active[1], 0)
            return pool.get_volume(curruuid)
        return None

    def pool_apply(self, src):
        pool = self.current_pool()
        if pool is None:
            return

        try:
            pool.set_autostart(self.window.get_widget("pool-autostart").get_active())
        except Exception, e:
            self.err.show_err(_("Error setting pool autostart: %s") % str(e),
                              "".join(traceback.format_exc()))
            return
        self.window.get_widget("pool-apply").set_sensitive(False)

    def pool_autostart_changed(self, src):
        auto = self.window.get_widget("pool-autostart").get_active()
        self.window.get_widget("pool-autostart").set_label(auto and \
                                                           _("On Boot") or \
                                                           _("Never"))
        self.window.get_widget("pool-apply").set_sensitive(True)

    def set_storage_error_page(self, msg):
        self.reset_pool_state()
        self.window.get_widget("storage-pages").set_current_page(1)
        self.window.get_widget("storage-error-label").set_text(msg)

    def pool_selected(self, src):
        selected = src.get_selected()
        if selected[1] is None or \
           selected[0].get_value(selected[1], 0) is None:
            self.set_storage_error_page(_("No storage pool selected."))
            return

        self.window.get_widget("storage-pages").set_current_page(0)
        self.window.get_widget("pool-apply").set_sensitive(False)
        uuid = selected[0].get_value(selected[1], 0)

        try:
            self.populate_pool_state(uuid)
        except Exception, e:
            logging.exception(e)
            self.set_storage_error_page(_("Error selecting pool: %s") % e)

    def populate_pool_state(self, uuid):
        pool = self.conn.get_pool(uuid)
        auto = pool.get_autostart()
        active = pool.is_active()

        # Set pool details state
        self.window.get_widget("pool-details").set_sensitive(True)
        self.window.get_widget("pool-name").set_markup("<b>%s:</b>" % \
                                                       pool.get_name())
        self.window.get_widget("pool-sizes").set_markup("""<span size="large">%s Free</span> / <i>%s In Use</i>""" % (pool.get_pretty_available(), pool.get_pretty_allocation()))
        self.window.get_widget("pool-type").set_text(Storage.StoragePool.get_pool_type_desc(pool.get_type()))
        self.window.get_widget("pool-location").set_text(pool.get_target_path())
        self.window.get_widget("pool-state-icon").set_from_pixbuf((active and self.PIXBUF_STATE_RUNNING) or self.PIXBUF_STATE_SHUTOFF)
        self.window.get_widget("pool-state").set_text((active and _("Active")) or _("Inactive"))
        self.window.get_widget("pool-autostart").set_label((auto and _("On Boot")) or _("Never"))
        self.window.get_widget("pool-autostart").set_active(auto)

        self.window.get_widget("vol-list").set_sensitive(active)
        self.populate_storage_volumes()

        self.window.get_widget("pool-delete").set_sensitive(not active)
        self.window.get_widget("pool-stop").set_sensitive(active)
        self.window.get_widget("pool-start").set_sensitive(not active)
        self.window.get_widget("vol-add").set_sensitive(active)
        self.window.get_widget("vol-delete").set_sensitive(False)

    def refresh_storage_pool(self, src, uri, uuid):
        refresh_pool_in_list(self.window.get_widget("pool-list"),
                             self.conn, uuid)
        curpool = self.current_pool()
        if curpool.uuid != uuid:
            return

        # Currently selected pool changed state: force a 'pool_selected' to
        # update vol list
        self.pool_selected(self.window.get_widget("pool-list").get_selection())

    def reset_pool_state(self):
        self.window.get_widget("pool-details").set_sensitive(False)
        self.window.get_widget("pool-name").set_text("")
        self.window.get_widget("pool-sizes").set_markup("""<span size="large"> </span>""")
        self.window.get_widget("pool-type").set_text("")
        self.window.get_widget("pool-location").set_text("")
        self.window.get_widget("pool-state-icon").set_from_pixbuf(self.PIXBUF_STATE_SHUTOFF)
        self.window.get_widget("pool-state").set_text(_("Inactive"))
        self.window.get_widget("vol-list").get_model().clear()
        self.window.get_widget("pool-autostart").set_label(_("Never"))
        self.window.get_widget("pool-autostart").set_active(False)

        self.window.get_widget("pool-delete").set_sensitive(False)
        self.window.get_widget("pool-stop").set_sensitive(False)
        self.window.get_widget("pool-start").set_sensitive(False)
        self.window.get_widget("pool-apply").set_sensitive(False)
        self.window.get_widget("vol-add").set_sensitive(False)
        self.window.get_widget("vol-delete").set_sensitive(False)
        self.window.get_widget("vol-list").set_sensitive(False)

    def vol_selected(self, src):
        selected = src.get_selected()
        if selected[1] is None or \
           selected[0].get_value(selected[1], 0) is None:
            self.window.get_widget("vol-delete").set_sensitive(False)
            return

        self.window.get_widget("vol-delete").set_sensitive(True)

    def popup_vol_menu(self, widget, event):
        if event.button != 3:
            return

        self.volmenu.popup(None, None, None, 0, event.time)

    def copy_vol_path(self, ignore=None):
        vol = self.current_vol()
        if not vol:
            return
        clipboard = gtk.Clipboard()
        target_path = vol.get_target_path()
        if target_path:
            clipboard.set_text(target_path)


    def repopulate_storage_pools(self, src, uri, uuid):
        pool_list = self.window.get_widget("pool-list")
        populate_storage_pools(pool_list, self.conn)

    def populate_storage_volumes(self):
        pool = self.current_pool()
        model = self.window.get_widget("vol-list").get_model()
        model.clear()
        vols = pool.get_volumes()
        for key in vols.keys():
            vol = vols[key]
            model.append([key, vol.get_name(), vol.get_pretty_capacity(),
                          vol.get_format() or ""])


    #############################
    # Interface manager methods #
    #############################

    def stop_interface(self, src):
        interface = self.current_interface()
        if interface is None:
            return

        do_prompt = self.config.get_confirm_interface()

        if do_prompt:
            res = self.err.warn_chkbox(
                    text1=_("Are you sure you want to stop the interface "
                            "'%s'?" % interface.get_name()),
                    chktext=_("Don't ask me again for interface start/stop."),
                    buttons=gtk.BUTTONS_YES_NO)

            response, skip_prompt = res
            if not response:
                return
            self.config.set_confirm_interface(not skip_prompt)

        try:
            interface.stop()
        except Exception, e:
            self.err.show_err(_("Error stopping interface '%s': %s") % \
                              (interface.get_name(), str(e)),
                              "".join(traceback.format_exc()))

    def start_interface(self, src):
        interface = self.current_interface()
        if interface is None:
            return

        do_prompt = self.config.get_confirm_interface()

        if do_prompt:
            res = self.err.warn_chkbox(
                    text1=_("Are you sure you want to start the interface "
                            "'%s'?" % interface.get_name()),
                    chktext=_("Don't ask me again for interface start/stop."),
                    buttons=gtk.BUTTONS_YES_NO)

            response, skip_prompt = res
            if not response:
                return
            self.config.set_confirm_interface(not skip_prompt)

        try:
            interface.start()
        except Exception, e:
            self.err.show_err(_("Error starting interface '%s': %s") % \
                              (interface.get_name(), str(e)),
                              "".join(traceback.format_exc()))

    def delete_interface(self, src):
        interface = self.current_interface()
        if interface is None:
            return

        result = self.err.yes_no(_("Are you sure you want to permanently "
                                   "delete the interface %s?")
                                   % interface.get_name())
        if not result:
            return

        try:
            interface.delete()
        except Exception, e:
            self.err.show_err(_("Error deleting interface: %s") % str(e),
                              "".join(traceback.format_exc()))

    def add_interface(self, src):
        try:
            if self.addinterface is None:
                self.addinterface = vmmCreateInterface(self.config, self.conn)
            self.addinterface.show()
        except Exception, e:
            self.err.show_err(_("Error launching interface wizard: %s") %
                              str(e), "".join(traceback.format_exc()))

    def refresh_current_interface(self, ignore1=None):
        cp = self.current_interface()
        if cp is None:
            return

        self.refresh_interface(None, None, cp.get_name())

    def current_interface(self):
        sel = self.window.get_widget("interface-list").get_selection()
        active = sel.get_selected()
        if active[1] != None:
            currname = active[0].get_value(active[1], 0)
            return self.conn.get_interface(currname)

        return None

    def interface_apply(self, src):
        interface = self.current_interface()
        if interface is None:
            return

        start_list = self.window.get_widget("interface-startmode")
        model = start_list.get_model()
        newmode = model[start_list.get_active()][0]

        try:
            interface.set_startmode(newmode)
        except Exception, e:
            self.err.show_err(_("Error setting interface startmode: %s") %
                              str(e), "".join(traceback.format_exc()))
            return

        # XXX: This will require an interface restart
        self.window.get_widget("interface-apply").set_sensitive(False)

    def interface_startmode_changed(self, src):
        self.window.get_widget("interface-apply").set_sensitive(True)

    def set_interface_error_page(self, msg):
        self.reset_interface_state()
        self.window.get_widget("interface-pages").set_current_page(
                                                        INTERFACE_PAGE_ERROR)
        self.window.get_widget("interface-error-label").set_text(msg)

    def interface_selected(self, src):
        selected = src.get_selected()
        if selected[1] is None or \
           selected[0].get_value(selected[1], 0) is None:
            self.set_interface_error_page(_("No interface selected."))
            return

        self.window.get_widget("interface-pages").set_current_page(
                                                        INTERFACE_PAGE_INFO)
        self.window.get_widget("interface-apply").set_sensitive(False)
        name = selected[0].get_value(selected[1], 0)

        try:
            self.populate_interface_state(name)
        except Exception, e:
            logging.exception(e)
            self.set_interface_error_page(_("Error selecting interface: %s") %
                                          e)

    def populate_interface_state(self, name):
        interface = self.conn.get_interface(name)
        children = interface.get_slaves()
        itype = interface.get_type()
        mac = interface.get_mac()
        active = interface.is_active()
        startmode = interface.get_startmode()

        self.window.get_widget("interface-details").set_sensitive(True)
        self.window.get_widget("interface-name").set_markup(
            "<b>%s %s:</b>" % (interface.get_pretty_type(),
                               interface.get_name()))
        self.window.get_widget("interface-mac").set_text(mac or _("Unknown"))

        self.window.get_widget("interface-state-icon").set_from_pixbuf(
            (active and self.PIXBUF_STATE_RUNNING) or self.PIXBUF_STATE_SHUTOFF)
        self.window.get_widget("interface-state").set_text(
                                    (active and _("Active")) or _("Inactive"))

        self.window.get_widget("interface-startmode").hide()
        self.window.get_widget("interface-startmode-label").show()
        self.window.get_widget("interface-startmode-label").set_text(startmode)

        used_by = util.iface_in_use_by(self.conn, name)
        self.window.get_widget("interface-inuseby").set_text(used_by or "-")

        self.window.get_widget("interface-delete").set_sensitive(not active)
        self.window.get_widget("interface-stop").set_sensitive(active)
        self.window.get_widget("interface-start").set_sensitive(not active)

        show_child = (children or
                      itype in [Interface.Interface.INTERFACE_TYPE_BRIDGE,
                                Interface.Interface.INTERFACE_TYPE_BOND])
        self.window.get_widget("interface-child-box").set_property("visible",
                                                                   show_child)
        self.populate_interface_children()

    def refresh_interface(self, src, uri, name):
        iface_list = self.window.get_widget("interface-list")
        sel = iface_list.get_selection()
        active = sel.get_selected()

        for row in iface_list.get_model():
            if row[0] == name:
                row[4] = self.conn.get_interface(name).is_active()

        if active[1] != None:
            currname = active[0].get_value(active[1], 0)
            if currname == name:
                self.interface_selected(sel)


    def reset_interface_state(self):
        if not self.conn.interface_capable:
            self.window.get_widget("interface-add").set_sensitive(False)
        self.window.get_widget("interface-delete").set_sensitive(False)
        self.window.get_widget("interface-stop").set_sensitive(False)
        self.window.get_widget("interface-start").set_sensitive(False)
        self.window.get_widget("interface-apply").set_sensitive(False)

    def repopulate_interfaces(self, src, uri, name):
        interface_list = self.window.get_widget("interface-list")
        self.populate_interfaces(interface_list.get_model())

    def populate_interfaces(self, model):
        iface_list = self.window.get_widget("interface-list")
        model.clear()
        for name in self.conn.list_interface_names():
            iface = self.conn.get_interface(name)
            model.append([name, iface.get_name(), "network-idle",
                          gtk.ICON_SIZE_LARGE_TOOLBAR,
                          bool(iface.is_active())])

        _iter = model.get_iter_first()
        if _iter:
            iface_list.get_selection().select_iter(_iter)
        iface_list.get_selection().emit("changed")

    def populate_interface_children(self):
        interface = self.current_interface()
        child_list = self.window.get_widget("interface-child-list")
        model = child_list.get_model()
        model.clear()

        if not interface:
            return

        for name, itype in interface.get_slaves():
            row = [name, itype]
            model.append(row)


# These functions are broken out, since they are used by storage browser
# dialog.

def init_pool_list(pool_list, changed_func):
    poolListModel = gtk.ListStore(str, str, bool, str)
    pool_list.set_model(poolListModel)

    pool_list.get_selection().connect("changed", changed_func)

    poolCol = gtk.TreeViewColumn("Storage Pools")
    pool_txt = gtk.CellRendererText()
    pool_per = gtk.CellRendererText()
    poolCol.pack_start(pool_per, False)
    poolCol.pack_start(pool_txt, True)
    poolCol.add_attribute(pool_txt, 'markup', 1)
    poolCol.add_attribute(pool_txt, 'sensitive', 2)
    poolCol.add_attribute(pool_per, 'markup', 3)
    pool_list.append_column(poolCol)
    poolListModel.set_sort_column_id(1, gtk.SORT_ASCENDING)

def refresh_pool_in_list(pool_list, conn, uuid):
    for row in pool_list.get_model():
        if row[0] == uuid:
            # Update active sensitivity and percent available for passed uuid
            row[3] = get_pool_size_percent(conn, uuid)
            row[2] = conn.get_pool(uuid).is_active()
            return

def populate_storage_pools(pool_list, conn):
    model = pool_list.get_model()
    model.clear()
    for uuid in conn.list_pool_uuids():
        per = get_pool_size_percent(conn, uuid)
        pool = conn.get_pool(uuid)

        name = pool.get_name()
        typ = Storage.StoragePool.get_pool_type_desc(pool.get_type())
        label = "%s\n<span size='small'>%s</span>" % (name, typ)

        model.append([uuid, label, pool.is_active(), per])

    _iter = model.get_iter_first()
    if _iter:
        pool_list.get_selection().select_iter(_iter)
    pool_list.get_selection().emit("changed")

def get_pool_size_percent(conn, uuid):
    pool = conn.get_pool(uuid)
    cap = pool.get_capacity()
    alloc = pool.get_allocation()
    if not cap or alloc is None:
        per = 0
    else:
        per = int(((float(alloc) / float(cap)) * 100))
    return "<span size='small' color='#484848'>%s%%</span>" % int(per)

gobject.type_register(vmmHost)
