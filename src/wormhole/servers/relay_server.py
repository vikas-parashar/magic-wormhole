from __future__ import print_function
import re, json, time, random
from twisted.python import log
from twisted.application import service, internet
from twisted.web import server, resource, http

SECONDS = 1.0
MINUTE = 60*SECONDS
HOUR = 60*MINUTE
DAY = 24*HOUR
MB = 1000*1000

CHANNEL_EXPIRATION_TIME = 3*DAY
EXPIRATION_CHECK_PERIOD = 2*HOUR

class EventsProtocol:
    def __init__(self, request):
        self.request = request

    def sendComment(self, comment):
        # this is ignored by clients, but can keep the connection open in the
        # face of firewall/NAT timeouts. It also helps unit tests, since
        # apparently twisted.web.client.Agent doesn't consider the connection
        # to be established until it sees the first byte of the reponse body.
        self.request.write(b": " + comment + b"\n\n")

    def sendEvent(self, data, name=None, id=None, retry=None):
        if name:
            self.request.write(b"event: " + name.encode("utf-8") + b"\n")
            # e.g. if name=foo, then the client web page should do:
            # (new EventSource(url)).addEventListener("foo", handlerfunc)
            # Note that this basically defaults to "message".
            self.request.write(b"\n")
        if id:
            self.request.write(b"id: " + id.encode("utf-8") + b"\n")
            self.request.write(b"\n")
        if retry:
            self.request.write(b"retry: " + retry + b"\n") # milliseconds
            self.request.write(b"\n")
        for line in data.splitlines():
            self.request.write(b"data: " + line.encode("utf-8") + b"\n")
        self.request.write(b"\n")

    def stop(self):
        self.request.finish()

# note: no versions of IE (including the current IE11) support EventSource

# relay URLs are:
#  GET /APPID/list                         -> {channel-ids: [INT..]}
#  POST /APPID/allocate {side: SIDE}       -> {channel-id: INT}
#   these return all messages (base64) for CID= :
#  POST /APPID/CID {side:, phase:, body:}  -> {messages: [{phase:, body:}..]}
#  GET  /APPID/CID (no-eventsource)        -> {messages: [{phase:, body:}..]}
#  GET  /APPID/CID (eventsource)           -> {phase:, body:}..
#  POST /APPID/CID/deallocate {side: SIDE} -> {status: waiting | deleted}
# all JSON responses include a "welcome:{..}" key

class Channel(resource.Resource):
    def __init__(self, app_id, channel_id, relay, db, welcome):
        resource.Resource.__init__(self)
        self.app_id = app_id
        self.channel_id = channel_id
        self.relay = relay
        self.db = db
        self.welcome = welcome
        self.event_channels = set() # ep
        self.putChild(b"deallocate", Deallocator(self.channel_id, self.relay))

    def get_messages(self, request):
        request.setHeader(b"content-type", b"application/json; charset=utf-8")
        messages = []
        for row in self.db.execute("SELECT * FROM `messages`"
                                   " WHERE `app_id`=? AND `channel_id`=?"
                                   " ORDER BY `when` ASC",
                                   (self.app_id, self.channel_id)).fetchall():
            messages.append({"phase": row["phase"], "body": row["body"]})
        data = {"welcome": self.welcome, "messages": messages}
        return (json.dumps(data)+"\n").encode("utf-8")

    def render_GET(self, request):
        if b"text/event-stream" not in (request.getHeader(b"accept") or b""):
            return self.get_messages(request)
        request.setHeader(b"content-type", b"text/event-stream; charset=utf-8")
        ep = EventsProtocol(request)
        ep.sendEvent(json.dumps(self.welcome), name="welcome")
        self.event_channels.add(ep)
        request.notifyFinish().addErrback(lambda f:
                                          self.event_channels.discard(ep))
        for row in self.db.execute("SELECT * FROM `messages`"
                                   " WHERE `app_id`=? AND `channel_id`=?"
                                   " ORDER BY `when` ASC",
                                   (self.app_id, self.channel_id)).fetchall():
            data = json.dumps({"phase": row["phase"], "body": row["body"]})
            ep.sendEvent(data)
        return server.NOT_DONE_YET

    def broadcast_message(self, phase, body):
        data = json.dumps({"phase": phase, "body": body})
        for ep in self.event_channels:
            ep.sendEvent(data)

    def render_POST(self, request):
        #data = json.load(request.content, encoding="utf-8")
        content = request.content.read()
        data = json.loads(content.decode("utf-8"))

        side = data["side"]
        phase = data["phase"]
        if not isinstance(phase, type(u"")):
            raise TypeError("phase must be string, not %s" % type(phase))
        body = data["body"]

        self.db.execute("INSERT INTO `messages`"
                        " (`app_id`, `channel_id`, `side`, `phase`,"
                        "  `body`, `when`)"
                        " VALUES (?,?,?,?, ?,?)",
                        (self.app_id, self.channel_id, side, phase,
                         body, time.time()))
        self.db.execute("INSERT INTO `allocations`"
                        " (`app_id`, `channel_id`, `side`)"
                        " VALUES (?,?,?)",
                        (self.app_id, self.channel_id, side))
        self.db.commit()
        self.broadcast_message(phase, body)
        return self.get_messages(request)

class Deallocator(resource.Resource):
    def __init__(self, channel_id, relay):
        self.channel_id = channel_id
        self.relay = relay

    def render_POST(self, request):
        content = request.content.read()
        data = json.loads(content.decode("utf-8"))
        side = data["side"]
        deleted = self.relay.maybe_free_child(self.channel_id, side)
        resp = {"status": "waiting"}
        if deleted:
            resp = {"status": "deleted"}
        return json.dumps(resp).encode("utf-8")

def get_allocated(db, app_id):
    c = db.execute("SELECT DISTINCT `channel_id` FROM `allocations`"
                   " WHERE `app_id`=?", (app_id,))
    return set([row["channel_id"] for row in c.fetchall()])

class Allocator(resource.Resource):
    def __init__(self, app_id, db, welcome):
        resource.Resource.__init__(self)
        self.app_id = app_id
        self.db = db
        self.welcome = welcome

    def allocate_channel_id(self):
        allocated = get_allocated(self.db, self.app_id)
        for size in range(1,4): # stick to 1-999 for now
            available = set()
            for cid in range(10**(size-1), 10**size):
                if cid not in allocated:
                    available.add(cid)
            if available:
                return random.choice(list(available))
        # ouch, 999 currently allocated. Try random ones for a while.
        for tries in range(1000):
            cid = random.randrange(1000, 1000*1000)
            if cid not in allocated:
                return cid
        raise ValueError("unable to find a free channel-id")

    def render_POST(self, request):
        content = request.content.read()
        data = json.loads(content.decode("utf-8"))
        side = data["side"]
        if not isinstance(side, type(u"")):
            raise TypeError("side must be string, not '%s'" % type(side))
        channel_id = self.allocate_channel_id()
        self.db.execute("INSERT INTO `allocations` VALUES (?,?,?)",
                        (self.app_id, channel_id, side))
        self.db.commit()
        log.msg("allocated #%d, now have %d DB channels" %
                (channel_id, len(get_allocated(self.db, self.app_id))))
        request.setHeader(b"content-type", b"application/json; charset=utf-8")
        data = {"welcome": self.welcome,
                "channel-id": channel_id}
        return (json.dumps(data)+"\n").encode("utf-8")

class ChannelList(resource.Resource):
    def __init__(self, app_id, db, welcome):
        resource.Resource.__init__(self)
        self.app_id = app_id
        self.db = db
        self.welcome = welcome
    def render_GET(self, request):
        allocated = get_allocated(self.db, self.app_id)
        request.setHeader(b"content-type", b"application/json; charset=utf-8")
        data = {"welcome": self.welcome,
                "channel-ids": sorted(allocated)}
        return (json.dumps(data)+"\n").encode("utf-8")

class AppNamespace(resource.Resource):
    def __init__(self, app_id, db, welcome):
        resource.Resource.__init__(self)
        self.app_id = app_id
        self.db = db
        self.welcome = welcome
        self.channels = {}

    def getChild(self, path, request):
        if path == b"allocate":
            return Allocator(self.app_id, self.db, self.welcome)
        if path == b"list":
            return ChannelList(self.app_id, self.db, self.welcome)
        if not re.search(br'^\d+$', path):
            return resource.ErrorPage(http.BAD_REQUEST,
                                      "invalid channel id",
                                      "invalid channel id")
        channel_id = int(path)
        if not channel_id in self.channels:
            log.msg("spawning #%d for appid %s" % (channel_id, self.app_id))
            self.channels[channel_id] = Channel(self.app_id, channel_id,
                                                self, self.db, self.welcome)
        return self.channels[channel_id]

    def maybe_free_child(self, channel_id, side):
        self.db.execute("DELETE FROM `allocations`"
                        " WHERE `app_id`=? AND `channel_id`=? AND `side`=?",
                        (self.app_id, channel_id, side))
        self.db.commit()
        remaining = self.db.execute("SELECT COUNT(*) FROM `allocations`"
                                    " WHERE `app_id`=? AND `channel_id`=?",
                                    (self.app_id, channel_id)).fetchone()[0]
        if remaining:
            return False
        self.free_child(channel_id)
        return True

    def free_child(self, channel_id):
        self.db.execute("DELETE FROM `allocations`"
                        " WHERE `app_id`=? AND `channel_id`=?",
                        (self.app_id, channel_id))
        self.db.execute("DELETE FROM `messages`"
                        " WHERE `app_id`=? AND `channel_id`=?",
                        (self.app_id, channel_id))
        self.db.commit()
        if channel_id in self.channels:
            self.channels.pop(channel_id)
        log.msg("freed+killed #%d, now have %d DB channels, %d live" %
                (channel_id,
                 len(get_allocated(self.db, self.app_id)),
                 len(self.channels)))

    def prune_old_channels(self):
        old = time.time() - CHANNEL_EXPIRATION_TIME
        for channel_id in get_allocated(self.db, self.app_id):
            c = self.db.execute("SELECT `when` FROM `messages`"
                                " WHERE `app_id`=? AND `channel_id`=?"
                                " ORDER BY `when` DESC LIMIT 1",
                                (self.app_id, channel_id))
            rows = c.fetchall()
            if not rows or (rows[0]["when"] < old):
                log.msg("expiring %d" % channel_id)
                self.free_child(channel_id)
        return bool(self.channels)

class Relay(resource.Resource, service.MultiService):
    def __init__(self, db, welcome):
        resource.Resource.__init__(self)
        service.MultiService.__init__(self)
        self.db = db
        self.welcome = welcome
        self.apps = {}
        t = internet.TimerService(EXPIRATION_CHECK_PERIOD, self.prune)
        t.setServiceParent(self)

    def getChild(self, app_id, request):
        if not app_id in self.apps:
            log.msg("spawning appid %s" % (app_id,))
            self.apps[app_id] = AppNamespace(app_id, self.db, self.welcome)
        return self.apps[app_id]

    def prune(self):
        for app_id in list(self.apps):
            still_active = self.apps[app_id].prune_old_channels()
            if not still_active:
                self.apps.pop(app_id)
