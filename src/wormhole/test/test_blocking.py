from twisted.trial import unittest
from twisted.internet import reactor, threads
from twisted.application import service
from .. import transcribe, relay


class Blocking(unittest.TestCase):
    def setUp(self):
        self.sparent = service.MultiService()
        self.sparent.startService()
    def tearDown(self):
        return self.sparent.stopService()

    def test_basic(self):
        rs = relay.RelayServer("tcp:0:interface=127.0.0.1")
        rs.setServiceParent(self.sparent)
        print rs.port_service

