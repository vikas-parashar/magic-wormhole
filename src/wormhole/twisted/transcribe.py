from __future__ import print_function
import os, sys, json, re
from binascii import hexlify, unhexlify
from zope.interface import implementer
from twisted.internet import reactor, defer
from twisted.web import client as web_client
from twisted.web import error as web_error
from twisted.web.iweb import IBodyProducer
from nacl.secret import SecretBox
from nacl.exceptions import CryptoError
from nacl import utils
from spake2 import SPAKE2_Symmetric
from .eventsource_twisted import ReconnectingEventSource
from .. import __version__
from .. import codes
from ..errors import ServerError, WrongPasswordError, UsageError
from ..util.hkdf import HKDF

@implementer(IBodyProducer)
class DataProducer:
    def __init__(self, data):
        self.data = data
        self.length = len(data)
    def startProducing(self, consumer):
        consumer.write(self.data)
        return defer.succeed(None)
    def stopProducing(self):
        pass
    def pauseProducing(self):
        pass
    def resumeProducing(self):
        pass


def post_json(agent, url, request_body):
    # POST a JSON body to a URL, parsing the response as JSON
    data = json.dumps(request_body).encode("utf-8")
    d = agent.request("POST", url, bodyProducer=DataProducer(data))
    def _check_error(resp):
        if resp.code != 200:
            raise web_error.Error(resp.code, resp.phrase)
        return resp
    d.addCallback(_check_error)
    d.addCallback(web_client.readBody)
    d.addCallback(lambda data: json.loads(data))
    return d

class Channel:
    def __init__(self, relay, app_id, channel_id, side, handle_welcome,
                 agent):
        self._channel_url = "%s%s/%d" % (relay, app_id, channel_id)
        self._side = side
        self._handle_welcome = handle_welcome
        self._agent = agent
        self._messages = set() # (phase,body) , body is bytes
        self._sent_messages = set() # (phase,body)

    def _add_inbound_messages(self, messages):
        for msg in messages:
            phase = msg["phase"]
            body = unhexlify(msg["body"].encode("ascii"))
            self._messages.add( (phase, body) )

    def _find_inbound_message(self, phase):
        for (their_phase,body) in self._messages - self._sent_messages:
            if their_phase == phase:
                return body
        return None

    def send(self, phase, msg):
        # TODO: retry on failure, with exponential backoff. We're guarding
        # against the rendezvous server being temporarily offline.
        if not isinstance(phase, type(u"")): raise UsageError(type(phase))
        if not isinstance(msg, type(b"")): raise UsageError(type(msg))
        self._sent_messages.add( (phase,msg) )
        payload = {"side": self._side,
                   "phase": phase,
                   "body": hexlify(msg).decode("ascii")}
        d = post_json(self._agent, self._channel_url, payload)
        d.addCallback(lambda resp: self._add_inbound_messages(resp["messages"]))
        return d

    def get(self, phase):
        # fire with a bytestring of the first message for 'phase' that wasn't
        # one of ours. It will either come from previously-received messages,
        # or from an EventSource that we attach to the corresponding URL
        body = self._find_inbound_message(phase)
        if body is not None:
            return defer.succeed(body)
        d = defer.Deferred()
        msgs = []
        def _handle(name, data):
            if name == "welcome":
                self._handle_welcome(json.loads(data))
            if name == "message":
                self._add_inbound_messages([json.loads(data)])
                body = self._find_inbound_message(phase)
                if body is not None and not msgs:
                    msgs.append(body)
                    d.callback(None)
        # TODO: use agent=self._agent
        es = ReconnectingEventSource(self._channel_url, _handle)
        es.startService() # TODO: .setServiceParent(self)
        es.activate()
        d.addCallback(lambda _: es.deactivate())
        d.addCallback(lambda _: es.stopService())
        d.addCallback(lambda _: msgs[0])
        return d

    def deallocate(self):
        # only try once, no retries
        d = post_json(self._agent, self._channel_url+"/deallocate",
                      {"side": self._side})
        d.addBoth(lambda _: None) # ignore POST failure
        return d

class ChannelManager:
    def __init__(self, relay, app_id, side, handle_welcome):
        self._relay = relay
        self._app_id = app_id
        self._side = side
        self._handle_welcome = handle_welcome
        self._agent = web_client.Agent(reactor)

    def allocate(self):
        url = self._relay + "%s/allocate" % self._app_id
        d = post_json(self._agent, url, {"side": self._side})
        def _got_channel(data):
            if "welcome" in data:
                self._handle_welcome(data["welcome"])
            return data["channel-id"]
        d.addCallback(_got_channel)
        return d

    def list_channels(self):
        raise NotImplementedError

    def connect(self, channel_id):
        return Channel(self._relay, self._app_id, channel_id, self._side,
                       self._handle_welcome, self._agent)

class Wormhole:
    motd_displayed = False
    version_warning_displayed = False

    def __init__(self, app_id, relay):
        if not isinstance(app_id, type(b"")): raise UsageError
        if b"/" in app_id: raise UsageError
        self._app_id = app_id
        self.relay = relay
        self._set_side(hexlify(os.urandom(5)).decode("ascii"))
        self.code = None
        self.key = None
        self._started_get_code = False
        self._sent_data = False
        self._got_data = False
        self._closed = False

    def _set_side(self, side):
        self._side = side
        self._channel_manager = ChannelManager(self.relay, self._app_id,
                                               self._side, self.handle_welcome)

    def handle_welcome(self, welcome):
        if ("motd" in welcome and
            not self.motd_displayed):
            motd_lines = welcome["motd"].splitlines()
            motd_formatted = "\n ".join(motd_lines)
            print("Server (at %s) says:\n %s" % (self.relay, motd_formatted),
                  file=sys.stderr)
            self.motd_displayed = True

        # Only warn if we're running a release version (e.g. 0.0.6, not
        # 0.0.6-DISTANCE-gHASH). Only warn once.
        if ("-" not in __version__ and
            not self.version_warning_displayed and
            welcome["current_version"] != __version__):
            print("Warning: errors may occur unless both sides are running the same version", file=sys.stderr)
            print("Server claims %s is current, but ours is %s"
                  % (welcome["current_version"], __version__), file=sys.stderr)
            self.version_warning_displayed = True

        if "error" in welcome:
            raise ServerError(welcome["error"], self.relay)

    def get_code(self, code_length=2):
        if self.code is not None: raise UsageError
        if self._started_get_code: raise UsageError
        self._started_get_code = True
        d = self._channel_manager.allocate()
        def _got_channel_id(channel_id):
            code = codes.make_code(channel_id, code_length)
            assert isinstance(code, str), type(code)
            self._set_code_and_channel_id(code)
            self._start()
            return code
        d.addCallback(_got_channel_id)
        return d

    def set_code(self, code):
        if not isinstance(code, str): raise UsageError
        if self.code is not None: raise UsageError
        self._set_code_and_channel_id(code)
        self._start()

    def _set_code_and_channel_id(self, code):
        if self.code is not None: raise UsageError
        mo = re.search(r'^(\d+)-', code)
        if not mo:
            raise ValueError("code (%s) must start with NN-" % code)
        self.code = code
        channel_id = int(mo.group(1))
        self.channel = self._channel_manager.connect(channel_id)

    def _start(self):
        # allocate the rest now too, so it can be serialized
        self.sp = SPAKE2_Symmetric(self.code.encode("ascii"),
                                   idSymmetric=self._app_id)
        self.msg1 = self.sp.start()

    def serialize(self):
        # I can only be serialized after get_code/set_code and before
        # get_verifier/get_data
        if self.code is None: raise UsageError
        if self.key is not None: raise UsageError
        if self._sent_data: raise UsageError
        if self._got_data: raise UsageError
        data = {
            "app_id": self._app_id,
            "relay": self.relay,
            "code": self.code,
            "side": self._side,
            "spake2": json.loads(self.sp.serialize()),
            "msg1": self.msg1.encode("hex"),
        }
        return json.dumps(data)

    @classmethod
    def from_serialized(klass, data):
        d = json.loads(data)
        self = klass(d["app_id"].encode("ascii"), d["relay"].encode("ascii"))
        self._set_side(d["side"].encode("ascii"))
        self._set_code_and_channel_id(d["code"].encode("ascii"))
        self.sp = SPAKE2_Symmetric.from_serialized(json.dumps(d["spake2"]))
        self.msg1 = d["msg1"].decode("hex")
        return self

    def derive_key(self, purpose, length=SecretBox.KEY_SIZE):
        if self.key is None:
            # call after get_verifier() or get_data()
            raise UsageError
        if not isinstance(purpose, type(b"")): raise UsageError
        return HKDF(self.key, length, CTXinfo=purpose)

    def _encrypt_data(self, key, data):
        assert isinstance(key, type(b"")), type(key)
        assert isinstance(data, type(b"")), type(data)
        if len(key) != SecretBox.KEY_SIZE: raise UsageError
        box = SecretBox(key)
        nonce = utils.random(SecretBox.NONCE_SIZE)
        return box.encrypt(data, nonce)

    def _decrypt_data(self, key, encrypted):
        assert isinstance(key, type(b"")), type(key)
        assert isinstance(encrypted, type(b"")), type(encrypted)
        if len(key) != SecretBox.KEY_SIZE: raise UsageError
        box = SecretBox(key)
        data = box.decrypt(encrypted)
        return data


    def _get_key(self):
        # TODO: prevent multiple invocation
        if self.key:
            return defer.succeed(self.key)
        d = self.channel.send(u"pake", self.msg1)
        d.addCallback(lambda _: self.channel.get(u"pake"))
        def _got_pake(pake_msg):
            key = self.sp.finish(pake_msg)
            self.key = key
            self.verifier = self.derive_key(self._app_id+b":Verifier")
            return key
        d.addCallback(_got_pake)
        return d

    def get_verifier(self):
        if self.code is None: raise UsageError
        d = self._get_key()
        d.addCallback(lambda _: self.verifier)
        return d

    def send_data(self, outbound_data):
        if self._sent_data: raise UsageError # only call this once
        if not isinstance(outbound_data, type(b"")): raise UsageError
        if self.code is None: raise UsageError
        if self.channel is None: raise UsageError
        # Without predefined roles, we can't derive predictably unique keys
        # for each side, so we use the same key for both. We use random
        # nonces to keep the messages distinct, and the Channel automatically
        # ignores reflections.
        d = self._get_key()
        def _send(key):
            data_key = self.derive_key(b"data-key")
            outbound_encrypted = self._encrypt_data(data_key, outbound_data)
            return self.channel.send(u"data", outbound_encrypted)
        d.addCallback(_send)
        return d

    def get_data(self):
        if self._got_data: raise UsageError # only call this once
        if self.code is None: raise UsageError
        if self.channel is None: raise UsageError
        d = self._get_key()
        def _get(key):
            data_key = self.derive_key(b"data-key")
            d1 = self.channel.get(u"data")
            def _decrypt(inbound_encrypted):
                try:
                    inbound_data = self._decrypt_data(data_key,
                                                      inbound_encrypted)
                    return inbound_data
                except CryptoError:
                    raise WrongPasswordError
            d1.addCallback(_decrypt)
            return d1
        d.addCallback(_get)
        return d

    def close(self, res=None):
        d = self.channel.deallocate()
        def _closed(_):
            self._closed = True
            return res
        d.addCallback(_closed)
        return d

    def __del__(self):
        if not self._closed:
            print("Error: a Wormhole instance was not closed", file=sys.stderr)
