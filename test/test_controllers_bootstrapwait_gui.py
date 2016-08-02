#!/usr/bin/env python
#
# tests controllers/bootstrapwait/gui.py
#
# Copyright 2016 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import unittest
#  from unittest.mock import ANY, call, MagicMock, patch, sentinel
from unittest.mock import patch, MagicMock

from conjureup.controllers.bootstrapwait.gui import BootstrapWaitController


class BootstrapwaitGUIRenderTestCase(unittest.TestCase):
    def setUp(self):
        self.controller = BootstrapWaitController()

        self.finish_patcher = patch(
            'conjureup.controllers.bootstrapwait.gui.'
            'BootstrapWaitController.finish')
        self.mock_finish = self.finish_patcher.start()

        self.view_patcher = patch(
            'conjureup.controllers.bootstrapwait.gui.BootstrapWaitView')
        self.view_patcher.start()
        self.app_patcher = patch(
            'conjureup.controllers.bootstrapwait.gui.app')
        mock_app = self.app_patcher.start()
        mock_app.ui = MagicMock(name="app.ui")

        self.eventloop_patcher = patch(
            'conjureup.controllers.bootstrapwait.gui.EventLoop')
        self.mock_eventloop = self.eventloop_patcher.start()

    def tearDown(self):
        self.finish_patcher.stop()
        self.view_patcher.stop()
        self.app_patcher.stop()
        self.eventloop_patcher.stop()

    def test_render(self):
        "call render"
        self.controller.render()


class BootstrapwaitGUIFinishTestCase(unittest.TestCase):
    def setUp(self):
        self.controller = BootstrapWaitController()

        self.controllers_patcher = patch(
            'conjureup.controllers.bootstrapwait.gui.controllers')
        self.mock_controllers = self.controllers_patcher.start()

        self.render_patcher = patch(
            'conjureup.controllers.bootstrapwait.gui.'
            'BootstrapWaitController.render')
        self.mock_render = self.render_patcher.start()
        self.app_patcher = patch(
            'conjureup.controllers.bootstrapwait.gui.app')
        self.mock_app = self.app_patcher.start()
        self.mock_app.ui = MagicMock(name="app.ui")

    def tearDown(self):
        self.controllers_patcher.stop()
        self.render_patcher.stop()
        self.app_patcher.stop()

    def test_finish(self):
        "call finish"
        self.controller.finish()
