# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for Perspective Broker module.

TODO: update protocol level tests to use new connection API, leaving
only specific tests for old API.
"""

# issue1195 TODOs: replace pump.pump() with something involving Deferreds.
# Clean up warning suppression.

import sys, os, time, gc

from cStringIO import StringIO
from zope.interface import implements, Interface

from twisted.python.versions import Version
from twisted.trial import unittest
from twisted.spread import pb, util, publish, jelly
from twisted.internet import protocol, main, reactor
from twisted.internet.error import ConnectionRefusedError
from twisted.internet.defer import Deferred, gatherResults, succeed
from twisted.protocols.policies import WrappingFactory
from twisted.python import failure, log
from twisted.cred.error import UnauthorizedLogin, UnhandledCredentials
from twisted.cred import portal, checkers, credentials


class Dummy(pb.Viewable):
    def view_doNothing(self, user):
        if isinstance(user, DummyPerspective):
            return 'hello world!'
        else:
            return 'goodbye, cruel world!'


class DummyPerspective(pb.Avatar):
    """
    An L{IPerspective} avatar which will be used in some tests.
    """
    def perspective_getDummyViewPoint(self):
        return Dummy()



class DummyRealm(object):
    implements(portal.IRealm)

    def requestAvatar(self, avatarId, mind, *interfaces):
        for iface in interfaces:
            if iface is pb.IPerspective:
                return iface, DummyPerspective(avatarId), lambda: None


class IOPump:
    """
    Utility to pump data between clients and servers for protocol testing.

    Perhaps this is a utility worthy of being in protocol.py?
    """
    def __init__(self, client, server, clientIO, serverIO):
        self.client = client
        self.server = server
        self.clientIO = clientIO
        self.serverIO = serverIO

    def flush(self):
        """
        Pump until there is no more input or output. This does not run any
        timers, so don't use it with any code that calls reactor.callLater.
        """
        # failsafe timeout
        timeout = time.time() + 5
        while self.pump():
            if time.time() > timeout:
                return

    def pump(self):
        """
        Move data back and forth.

        Returns whether any data was moved.
        """
        self.clientIO.seek(0)
        self.serverIO.seek(0)
        cData = self.clientIO.read()
        sData = self.serverIO.read()
        self.clientIO.seek(0)
        self.serverIO.seek(0)
        self.clientIO.truncate()
        self.serverIO.truncate()
        self.client.transport._checkProducer()
        self.server.transport._checkProducer()
        for byte in cData:
            self.server.dataReceived(byte)
        for byte in sData:
            self.client.dataReceived(byte)
        if cData or sData:
            return 1
        else:
            return 0


def connectedServerAndClient():
    """
    Returns a 3-tuple: (client, server, pump).
    """
    clientBroker = pb.Broker()
    checker = checkers.InMemoryUsernamePasswordDatabaseDontUse(guest='guest')
    factory = pb.PBServerFactory(portal.Portal(DummyRealm(), [checker]))
    serverBroker = factory.buildProtocol(('127.0.0.1',))

    clientTransport = StringIO()
    serverTransport = StringIO()
    clientBroker.makeConnection(protocol.FileWrapper(clientTransport))
    serverBroker.makeConnection(protocol.FileWrapper(serverTransport))
    pump = IOPump(clientBroker, serverBroker, clientTransport, serverTransport)
    # Challenge-response authentication:
    pump.flush()
    return clientBroker, serverBroker, pump


class SimpleRemote(pb.Referenceable):
    def remote_thunk(self, arg):
        self.arg = arg
        return arg + 1

    def remote_knuth(self, arg):
        raise Exception()


class NestedRemote(pb.Referenceable):
    def remote_getSimple(self):
        return SimpleRemote()


class SimpleCopy(pb.Copyable):
    def __init__(self):
        self.x = 1
        self.y = {"Hello":"World"}
        self.z = ['test']


class SimpleLocalCopy(pb.RemoteCopy):
    pass

pb.setUnjellyableForClass(SimpleCopy, SimpleLocalCopy)


class SimpleFactoryCopy(pb.Copyable):
    """
    @cvar allIDs: hold every created instances of this class.
    @type allIDs: C{dict}
    """
    allIDs = {}
    def __init__(self, id):
        self.id = id
        SimpleFactoryCopy.allIDs[id] = self


def createFactoryCopy(state):
    """
    Factory of L{SimpleFactoryCopy}, getting a created instance given the
    C{id} found in C{state}.
    """
    stateId = state.get("id", None)
    if stateId is None:
        raise RuntimeError("factory copy state has no 'id' member %s" %
                           (repr(state),))
    if not stateId in SimpleFactoryCopy.allIDs:
        raise RuntimeError("factory class has no ID: %s" %
                           (SimpleFactoryCopy.allIDs,))
    inst = SimpleFactoryCopy.allIDs[stateId]
    if not inst:
        raise RuntimeError("factory method found no object with id")
    return inst

pb.setUnjellyableFactoryForClass(SimpleFactoryCopy, createFactoryCopy)


class NestedCopy(pb.Referenceable):
    def remote_getCopy(self):
        return SimpleCopy()

    def remote_getFactory(self, value):
        return SimpleFactoryCopy(value)



class SimpleCache(pb.Cacheable):
    def __init___(self):
        self.x = 1
        self.y = {"Hello":"World"}
        self.z = ['test']


class NestedComplicatedCache(pb.Referenceable):
    def __init__(self):
        self.c = VeryVeryComplicatedCacheable()

    def remote_getCache(self):
        return self.c


class VeryVeryComplicatedCacheable(pb.Cacheable):
    def __init__(self):
        self.x = 1
        self.y = 2
        self.foo = 3

    def setFoo4(self):
        self.foo = 4
        self.observer.callRemote('foo',4)

    def getStateToCacheAndObserveFor(self, perspective, observer):
        self.observer = observer
        return {"x": self.x,
                "y": self.y,
                "foo": self.foo}

    def stoppedObserving(self, perspective, observer):
        log.msg("stopped observing")
        observer.callRemote("end")
        if observer == self.observer:
            self.observer = None


class RatherBaroqueCache(pb.RemoteCache):
    def observe_foo(self, newFoo):
        self.foo = newFoo

    def observe_end(self):
        log.msg("the end of things")

pb.setUnjellyableForClass(VeryVeryComplicatedCacheable, RatherBaroqueCache)


class SimpleLocalCache(pb.RemoteCache):
    def setCopyableState(self, state):
        self.__dict__.update(state)

    def checkMethod(self):
        return self.check

    def checkSelf(self):
        return self

    def check(self):
        return 1

pb.setUnjellyableForClass(SimpleCache, SimpleLocalCache)


class NestedCache(pb.Referenceable):
    def __init__(self):
        self.x = SimpleCache()

    def remote_getCache(self):
        return [self.x,self.x]

    def remote_putCache(self, cache):
        return (self.x is cache)


class Observable(pb.Referenceable):
    def __init__(self):
        self.observers = []

    def remote_observe(self, obs):
        self.observers.append(obs)

    def remote_unobserve(self, obs):
        self.observers.remove(obs)

    def notify(self, obj):
        for observer in self.observers:
            observer.callRemote('notify', self, obj)


class DeferredRemote(pb.Referenceable):
    def __init__(self):
        self.run = 0

    def runMe(self, arg):
        self.run = arg
        return arg + 1

    def dontRunMe(self, arg):
        assert 0, "shouldn't have been run!"

    def remote_doItLater(self):
        """
        Return a L{Deferred} to be fired on client side. When fired,
        C{self.runMe} is called.
        """
        d = Deferred()
        d.addCallbacks(self.runMe, self.dontRunMe)
        self.d = d
        return d


class Observer(pb.Referenceable):
    notified = 0
    obj = None
    def remote_notify(self, other, obj):
        self.obj = obj
        self.notified = self.notified + 1
        other.callRemote('unobserve',self)


class NewStyleCopy(pb.Copyable, pb.RemoteCopy, object):
    def __init__(self, s):
        self.s = s
pb.setUnjellyableForClass(NewStyleCopy, NewStyleCopy)


class NewStyleCopy2(pb.Copyable, pb.RemoteCopy, object):
    allocated = 0
    initialized = 0
    value = 1

    def __new__(self):
        NewStyleCopy2.allocated += 1
        inst = object.__new__(self)
        inst.value = 2
        return inst

    def __init__(self):
        NewStyleCopy2.initialized += 1

pb.setUnjellyableForClass(NewStyleCopy2, NewStyleCopy2)


class NewStyleCacheCopy(pb.Cacheable, pb.RemoteCache, object):
    def getStateToCacheAndObserveFor(self, perspective, observer):
        return self.__dict__

pb.setUnjellyableForClass(NewStyleCacheCopy, NewStyleCacheCopy)


class Echoer(pb.Root):
    def remote_echo(self, st):
        return st


class CachedReturner(pb.Root):
    def __init__(self, cache):
        self.cache = cache
    def remote_giveMeCache(self, st):
        return self.cache


class NewStyleTestCase(unittest.TestCase):
    def setUp(self):
        """
        Create a pb server using L{Echoer} protocol and connect a client to it.
        """
        self.serverFactory = pb.PBServerFactory(Echoer())
        self.wrapper = WrappingFactory(self.serverFactory)
        self.server = reactor.listenTCP(0, self.wrapper)
        clientFactory = pb.PBClientFactory()
        reactor.connectTCP("localhost", self.server.getHost().port,
                           clientFactory)
        def gotRoot(ref):
            self.ref = ref
        return clientFactory.getRootObject().addCallback(gotRoot)


    def tearDown(self):
        """
        Close client and server connections, reset values of L{NewStyleCopy2}
        class variables.
        """
        NewStyleCopy2.allocated = 0
        NewStyleCopy2.initialized = 0
        NewStyleCopy2.value = 1
        self.ref.broker.transport.loseConnection()
        # Disconnect any server-side connections too.
        for proto in self.wrapper.protocols:
            proto.transport.loseConnection()
        return self.server.stopListening()

    def test_newStyle(self):
        """
        Create a new style object, send it over the wire, and check the result.
        """
        orig = NewStyleCopy("value")
        d = self.ref.callRemote("echo", orig)
        def cb(res):
            self.failUnless(isinstance(res, NewStyleCopy))
            self.failUnlessEqual(res.s, "value")
            self.failIf(res is orig) # no cheating :)
        d.addCallback(cb)
        return d

    def test_alloc(self):
        """
        Send a new style object and check the number of allocations.
        """
        orig = NewStyleCopy2()
        self.failUnlessEqual(NewStyleCopy2.allocated, 1)
        self.failUnlessEqual(NewStyleCopy2.initialized, 1)
        d = self.ref.callRemote("echo", orig)
        def cb(res):
            # receiving the response creates a third one on the way back
            self.failUnless(isinstance(res, NewStyleCopy2))
            self.failUnlessEqual(res.value, 2)
            self.failUnlessEqual(NewStyleCopy2.allocated, 3)
            self.failUnlessEqual(NewStyleCopy2.initialized, 1)
            self.failIf(res is orig) # no cheating :)
        # sending the object creates a second one on the far side
        d.addCallback(cb)
        return d



class ConnectionNotifyServerFactory(pb.PBServerFactory):
    """
    A server factory which stores the last connection and fires a
    L{Deferred} on connection made. This factory can handle only one
    client connection.

    @ivar protocolInstance: the last protocol instance.
    @type protocolInstance: C{pb.Broker}

    @ivar connectionMade: the deferred fired upon connection.
    @type connectionMade: C{Deferred}
    """
    protocolInstance = None

    def __init__(self, root):
        """
        Initialize the factory.
        """
        pb.PBServerFactory.__init__(self, root)
        self.connectionMade = Deferred()


    def clientConnectionMade(self, protocol):
        """
        Store the protocol and fire the connection deferred.
        """
        self.protocolInstance = protocol
        d, self.connectionMade = self.connectionMade, None
        if d is not None:
            d.callback(None)



class NewStyleCachedTestCase(unittest.TestCase):
    def setUp(self):
        """
        Create a pb server using L{CachedReturner} protocol and connect a
        client to it.
        """
        self.orig = NewStyleCacheCopy()
        self.orig.s = "value"
        self.server = reactor.listenTCP(0,
            ConnectionNotifyServerFactory(CachedReturner(self.orig)))
        clientFactory = pb.PBClientFactory()
        reactor.connectTCP("localhost", self.server.getHost().port,
                           clientFactory)
        def gotRoot(ref):
            self.ref = ref
        d1 = clientFactory.getRootObject().addCallback(gotRoot)
        d2 = self.server.factory.connectionMade
        return gatherResults([d1, d2])


    def tearDown(self):
        """
        Close client and server connections.
        """
        self.server.factory.protocolInstance.transport.loseConnection()
        self.ref.broker.transport.loseConnection()
        return self.server.stopListening()


    def test_newStyleCache(self):
        """
        Get the object from the cache, and checks its properties.
        """
        d = self.ref.callRemote("giveMeCache", self.orig)
        def cb(res):
            self.failUnless(isinstance(res, NewStyleCacheCopy))
            self.failUnlessEqual(res.s, "value")
            self.failIf(res is self.orig) # no cheating :)
        d.addCallback(cb)
        return d



class BrokerTestCase(unittest.TestCase):
    thunkResult = None

    def tearDown(self):
        try:
            # from RemotePublished.getFileName
            os.unlink('None-None-TESTING.pub')
        except OSError:
            pass

    def thunkErrorBad(self, error):
        self.fail("This should cause a return value, not %s" % (error,))

    def thunkResultGood(self, result):
        self.thunkResult = result

    def thunkErrorGood(self, tb):
        pass

    def thunkResultBad(self, result):
        self.fail("This should cause an error, not %s" % (result,))

    def test_reference(self):
        c, s, pump = connectedServerAndClient()

        class X(pb.Referenceable):
            def remote_catch(self,arg):
                self.caught = arg

        class Y(pb.Referenceable):
            def remote_throw(self, a, b):
                a.callRemote('catch', b)

        s.setNameForLocal("y", Y())
        y = c.remoteForName("y")
        x = X()
        z = X()
        y.callRemote('throw', x, z)
        pump.pump()
        pump.pump()
        pump.pump()
        self.assertIdentical(x.caught, z, "X should have caught Z")

        # make sure references to remote methods are equals
        self.assertEquals(y.remoteMethod('throw'), y.remoteMethod('throw'))

    def test_result(self):
        c, s, pump = connectedServerAndClient()
        for x, y in (c, s), (s, c):
            # test reflexivity
            foo = SimpleRemote()
            x.setNameForLocal("foo", foo)
            bar = y.remoteForName("foo")
            self.expectedThunkResult = 8
            bar.callRemote('thunk',self.expectedThunkResult - 1
                ).addCallbacks(self.thunkResultGood, self.thunkErrorBad)
            # Send question.
            pump.pump()
            # Send response.
            pump.pump()
            # Shouldn't require any more pumping than that...
            self.assertEquals(self.thunkResult, self.expectedThunkResult,
                              "result wasn't received.")

    def refcountResult(self, result):
        self.nestedRemote = result

    def test_tooManyRefs(self):
        l = []
        e = []
        c, s, pump = connectedServerAndClient()
        foo = NestedRemote()
        s.setNameForLocal("foo", foo)
        x = c.remoteForName("foo")
        for igno in xrange(pb.MAX_BROKER_REFS + 10):
            if s.transport.closed or c.transport.closed:
                break
            x.callRemote("getSimple").addCallbacks(l.append, e.append)
            pump.pump()
        expected = (pb.MAX_BROKER_REFS - 1)
        self.assertTrue(s.transport.closed, "transport was not closed")
        self.assertEquals(len(l), expected,
                          "expected %s got %s" % (expected, len(l)))

    def test_copy(self):
        c, s, pump = connectedServerAndClient()
        foo = NestedCopy()
        s.setNameForLocal("foo", foo)
        x = c.remoteForName("foo")
        x.callRemote('getCopy'
            ).addCallbacks(self.thunkResultGood, self.thunkErrorBad)
        pump.pump()
        pump.pump()
        self.assertEquals(self.thunkResult.x, 1)
        self.assertEquals(self.thunkResult.y['Hello'], 'World')
        self.assertEquals(self.thunkResult.z[0], 'test')

    def test_observe(self):
        c, s, pump = connectedServerAndClient()

        # this is really testing the comparison between remote objects, to make
        # sure that you can *UN*observe when you have an observer architecture.
        a = Observable()
        b = Observer()
        s.setNameForLocal("a", a)
        ra = c.remoteForName("a")
        ra.callRemote('observe',b)
        pump.pump()
        a.notify(1)
        pump.pump()
        pump.pump()
        a.notify(10)
        pump.pump()
        pump.pump()
        self.assertNotIdentical(b.obj, None, "didn't notify")
        self.assertEquals(b.obj, 1, 'notified too much')

    def test_defer(self):
        c, s, pump = connectedServerAndClient()
        d = DeferredRemote()
        s.setNameForLocal("d", d)
        e = c.remoteForName("d")
        pump.pump(); pump.pump()
        results = []
        e.callRemote('doItLater').addCallback(results.append)
        pump.pump(); pump.pump()
        self.assertFalse(d.run, "Deferred method run too early.")
        d.d.callback(5)
        self.assertEquals(d.run, 5, "Deferred method run too late.")
        pump.pump(); pump.pump()
        self.assertEquals(results[0], 6, "Incorrect result.")


    def test_refcount(self):
        c, s, pump = connectedServerAndClient()
        foo = NestedRemote()
        s.setNameForLocal("foo", foo)
        bar = c.remoteForName("foo")
        bar.callRemote('getSimple'
            ).addCallbacks(self.refcountResult, self.thunkErrorBad)

        # send question
        pump.pump()
        # send response
        pump.pump()

        # delving into internal structures here, because GC is sort of
        # inherently internal.
        rluid = self.nestedRemote.luid
        self.assertIn(rluid, s.localObjects)
        del self.nestedRemote
        # nudge the gc
        if sys.hexversion >= 0x2000000:
            gc.collect()
        # try to nudge the GC even if we can't really
        pump.pump()
        pump.pump()
        pump.pump()
        self.assertNotIn(rluid, s.localObjects)

    def test_cache(self):
        c, s, pump = connectedServerAndClient()
        obj = NestedCache()
        obj2 = NestedComplicatedCache()
        vcc = obj2.c
        s.setNameForLocal("obj", obj)
        s.setNameForLocal("xxx", obj2)
        o2 = c.remoteForName("obj")
        o3 = c.remoteForName("xxx")
        coll = []
        o2.callRemote("getCache"
            ).addCallback(coll.append).addErrback(coll.append)
        o2.callRemote("getCache"
            ).addCallback(coll.append).addErrback(coll.append)
        complex = []
        o3.callRemote("getCache").addCallback(complex.append)
        o3.callRemote("getCache").addCallback(complex.append)
        pump.flush()
        # `worst things first'
        self.assertEquals(complex[0].x, 1)
        self.assertEquals(complex[0].y, 2)
        self.assertEquals(complex[0].foo, 3)

        vcc.setFoo4()
        pump.flush()
        self.assertEquals(complex[0].foo, 4)
        self.assertEquals(len(coll), 2)
        cp = coll[0][0]
        self.assertIdentical(cp.checkMethod().im_self, cp,
                             "potential refcounting issue")
        self.assertIdentical(cp.checkSelf(), cp,
                             "other potential refcounting issue")
        col2 = []
        o2.callRemote('putCache',cp).addCallback(col2.append)
        pump.flush()
        # The objects were the same (testing lcache identity)
        self.assertTrue(col2[0])
        # test equality of references to methods
        self.assertEquals(o2.remoteMethod("getCache"),
                          o2.remoteMethod("getCache"))

        # now, refcounting (similiar to testRefCount)
        luid = cp.luid
        baroqueLuid = complex[0].luid
        self.assertIn(luid, s.remotelyCachedObjects,
                      "remote cache doesn't have it")
        del coll
        del cp
        pump.flush()
        del complex
        del col2
        # extra nudge...
        pump.flush()
        # del vcc.observer
        # nudge the gc
        if sys.hexversion >= 0x2000000:
            gc.collect()
        # try to nudge the GC even if we can't really
        pump.flush()
        # The GC is done with it.
        self.assertNotIn(luid, s.remotelyCachedObjects,
                         "Server still had it after GC")
        self.assertNotIn(luid, c.locallyCachedObjects,
                         "Client still had it after GC")
        self.assertNotIn(baroqueLuid, s.remotelyCachedObjects,
                         "Server still had complex after GC")
        self.assertNotIn(baroqueLuid, c.locallyCachedObjects,
                         "Client still had complex after GC")
        self.assertIdentical(vcc.observer, None, "observer was not removed")

    def test_publishable(self):
        try:
            os.unlink('None-None-TESTING.pub') # from RemotePublished.getFileName
        except OSError:
            pass # Sometimes it's not there.
        c, s, pump = connectedServerAndClient()
        foo = GetPublisher()
        # foo.pub.timestamp = 1.0
        s.setNameForLocal("foo", foo)
        bar = c.remoteForName("foo")
        accum = []
        bar.callRemote('getPub').addCallbacks(accum.append, self.thunkErrorBad)
        pump.flush()
        obj = accum.pop()
        self.assertEquals(obj.activateCalled, 1)
        self.assertEquals(obj.isActivated, 1)
        self.assertEquals(obj.yayIGotPublished, 1)
        # timestamp's dirty, we don't have a cache file
        self.assertEquals(obj._wasCleanWhenLoaded, 0)
        c, s, pump = connectedServerAndClient()
        s.setNameForLocal("foo", foo)
        bar = c.remoteForName("foo")
        bar.callRemote('getPub').addCallbacks(accum.append, self.thunkErrorBad)
        pump.flush()
        obj = accum.pop()
        # timestamp's clean, our cache file is up-to-date
        self.assertEquals(obj._wasCleanWhenLoaded, 1)

    def gotCopy(self, val):
        self.thunkResult = val.id


    def test_factoryCopy(self):
        c, s, pump = connectedServerAndClient()
        ID = 99
        obj = NestedCopy()
        s.setNameForLocal("foo", obj)
        x = c.remoteForName("foo")
        x.callRemote('getFactory', ID
            ).addCallbacks(self.gotCopy, self.thunkResultBad)
        pump.pump()
        pump.pump()
        pump.pump()
        self.assertEquals(self.thunkResult, ID,
            "ID not correct on factory object %s" % (self.thunkResult,))


bigString = "helloworld" * 50

callbackArgs = None
callbackKeyword = None

def finishedCallback(*args, **kw):
    global callbackArgs, callbackKeyword
    callbackArgs = args
    callbackKeyword = kw


class Pagerizer(pb.Referenceable):
    def __init__(self, callback, *args, **kw):
        self.callback, self.args, self.kw = callback, args, kw

    def remote_getPages(self, collector):
        util.StringPager(collector, bigString, 100,
                         self.callback, *self.args, **self.kw)
        self.args = self.kw = None


class FilePagerizer(pb.Referenceable):
    pager = None

    def __init__(self, filename, callback, *args, **kw):
        self.filename = filename
        self.callback, self.args, self.kw = callback, args, kw

    def remote_getPages(self, collector):
        self.pager = util.FilePager(collector, file(self.filename),
                                    self.callback, *self.args, **self.kw)
        self.args = self.kw = None



class PagingTestCase(unittest.TestCase):
    """
    Test pb objects sending data by pages.
    """

    def setUp(self):
        """
        Create a file used to test L{util.FilePager}.
        """
        self.filename = self.mktemp()
        fd = file(self.filename, 'w')
        fd.write(bigString)
        fd.close()


    def test_pagingWithCallback(self):
        """
        Test L{util.StringPager}, passing a callback to fire when all pages
        are sent.
        """
        c, s, pump = connectedServerAndClient()
        s.setNameForLocal("foo", Pagerizer(finishedCallback, 'hello', value=10))
        x = c.remoteForName("foo")
        l = []
        util.getAllPages(x, "getPages").addCallback(l.append)
        while not l:
            pump.pump()
        self.assertEquals(''.join(l[0]), bigString,
                          "Pages received not equal to pages sent!")
        self.assertEquals(callbackArgs, ('hello',),
                          "Completed callback not invoked")
        self.assertEquals(callbackKeyword, {'value': 10},
                          "Completed callback not invoked")


    def test_pagingWithoutCallback(self):
        """
        Test L{util.StringPager} without a callback.
        """
        c, s, pump = connectedServerAndClient()
        s.setNameForLocal("foo", Pagerizer(None))
        x = c.remoteForName("foo")
        l = []
        util.getAllPages(x, "getPages").addCallback(l.append)
        while not l:
            pump.pump()
        self.assertEquals(''.join(l[0]), bigString,
                          "Pages received not equal to pages sent!")


    def test_emptyFilePaging(self):
        """
        Test L{util.FilePager}, sending an empty file.
        """
        filenameEmpty = self.mktemp()
        fd = file(filenameEmpty, 'w')
        fd.close()
        c, s, pump = connectedServerAndClient()
        pagerizer = FilePagerizer(filenameEmpty, None)
        s.setNameForLocal("bar", pagerizer)
        x = c.remoteForName("bar")
        l = []
        util.getAllPages(x, "getPages").addCallback(l.append)
        ttl = 10
        while not l and ttl > 0:
            pump.pump()
            ttl -= 1
        if not ttl:
            self.fail('getAllPages timed out')
        self.assertEquals(''.join(l[0]), '',
                          "Pages received not equal to pages sent!")


    def test_filePagingWithCallback(self):
        """
        Test L{util.FilePager}, passing a callback to fire when all pages
        are sent, and verify that the pager doesn't keep chunks in memory.
        """
        c, s, pump = connectedServerAndClient()
        pagerizer = FilePagerizer(self.filename, finishedCallback,
                                  'frodo', value = 9)
        s.setNameForLocal("bar", pagerizer)
        x = c.remoteForName("bar")
        l = []
        util.getAllPages(x, "getPages").addCallback(l.append)
        while not l:
            pump.pump()
        self.assertEquals(''.join(l[0]), bigString,
                          "Pages received not equal to pages sent!")
        self.assertEquals(callbackArgs, ('frodo',),
                          "Completed callback not invoked")
        self.assertEquals(callbackKeyword, {'value': 9},
                          "Completed callback not invoked")
        self.assertEquals(pagerizer.pager.chunks, [])


    def test_filePagingWithoutCallback(self):
        """
        Test L{util.FilePager} without a callback.
        """
        c, s, pump = connectedServerAndClient()
        pagerizer = FilePagerizer(self.filename, None)
        s.setNameForLocal("bar", pagerizer)
        x = c.remoteForName("bar")
        l = []
        util.getAllPages(x, "getPages").addCallback(l.append)
        while not l:
            pump.pump()
        self.assertEquals(''.join(l[0]), bigString,
                          "Pages received not equal to pages sent!")
        self.assertEquals(pagerizer.pager.chunks, [])



class DumbPublishable(publish.Publishable):
    def getStateToPublish(self):
        return {"yayIGotPublished": 1}


class DumbPub(publish.RemotePublished):
    def activated(self):
        self.activateCalled = 1


class GetPublisher(pb.Referenceable):
    def __init__(self):
        self.pub = DumbPublishable("TESTING")

    def remote_getPub(self):
        return self.pub


pb.setUnjellyableForClass(DumbPublishable, DumbPub)

class DisconnectionTestCase(unittest.TestCase):
    """
    Test disconnection callbacks.
    """

    def error(self, *args):
        raise RuntimeError("I shouldn't have been called: %s" % (args,))


    def gotDisconnected(self):
        """
        Called on broker disconnect.
        """
        self.gotCallback = 1

    def objectDisconnected(self, o):
        """
        Called on RemoteReference disconnect.
        """
        self.assertEquals(o, self.remoteObject)
        self.objectCallback = 1

    def test_badSerialization(self):
        c, s, pump = connectedServerAndClient()
        pump.pump()
        s.setNameForLocal("o", BadCopySet())
        g = c.remoteForName("o")
        l = []
        g.callRemote("setBadCopy", BadCopyable()).addErrback(l.append)
        pump.flush()
        self.assertEquals(len(l), 1)

    def test_disconnection(self):
        c, s, pump = connectedServerAndClient()
        pump.pump()
        s.setNameForLocal("o", SimpleRemote())

        # get a client reference to server object
        r = c.remoteForName("o")
        pump.pump()
        pump.pump()
        pump.pump()

        # register and then unregister disconnect callbacks
        # making sure they get unregistered
        c.notifyOnDisconnect(self.error)
        self.assertIn(self.error, c.disconnects)
        c.dontNotifyOnDisconnect(self.error)
        self.assertNotIn(self.error, c.disconnects)

        r.notifyOnDisconnect(self.error)
        self.assertIn(r._disconnected, c.disconnects)
        self.assertIn(self.error, r.disconnectCallbacks)
        r.dontNotifyOnDisconnect(self.error)
        self.assertNotIn(r._disconnected, c.disconnects)
        self.assertNotIn(self.error, r.disconnectCallbacks)

        # register disconnect callbacks
        c.notifyOnDisconnect(self.gotDisconnected)
        r.notifyOnDisconnect(self.objectDisconnected)
        self.remoteObject = r

        # disconnect
        c.connectionLost(failure.Failure(main.CONNECTION_DONE))
        self.assertTrue(self.gotCallback)
        self.assertTrue(self.objectCallback)


class FreakOut(Exception):
    pass


class BadCopyable(pb.Copyable):
    def getStateToCopyFor(self, p):
        raise FreakOut()


class BadCopySet(pb.Referenceable):
    def remote_setBadCopy(self, bc):
        return None


class LocalRemoteTest(util.LocalAsRemote):
    reportAllTracebacks = 0

    def sync_add1(self, x):
        return x + 1

    def async_add(self, x=0, y=1):
        return x + y

    def async_fail(self):
        raise RuntimeError()



class MyPerspective(pb.Avatar):
    """
    @ivar loggedIn: set to C{True} when the avatar is logged in.
    @type loggedIn: C{bool}

    @ivar loggedOut: set to C{True} when the avatar is logged out.
    @type loggedOut: C{bool}
    """
    implements(pb.IPerspective)

    loggedIn = loggedOut = False

    def __init__(self, avatarId):
        self.avatarId = avatarId


    def perspective_getAvatarId(self):
        """
        Return the avatar identifier which was used to access this avatar.
        """
        return self.avatarId


    def perspective_getViewPoint(self):
        return MyView()


    def perspective_add(self, a, b):
        """
        Add the given objects and return the result.  This is a method
        unavailable on L{Echoer}, so it can only be invoked by authenticated
        users who received their avatar from L{TestRealm}.
        """
        return a + b


    def logout(self):
        self.loggedOut = True



class TestRealm(object):
    """
    A realm which repeatedly gives out a single instance of L{MyPerspective}
    for non-anonymous logins and which gives out a new instance of L{Echoer}
    for each anonymous login.

    @ivar lastPerspective: The L{MyPerspective} most recently created and
        returned from C{requestAvatar}.

    @ivar perspectiveFactory: A one-argument callable which will be used to
        create avatars to be returned from C{requestAvatar}.
    """
    perspectiveFactory = MyPerspective

    lastPerspective = None

    def requestAvatar(self, avatarId, mind, interface):
        """
        Verify that the mind and interface supplied have the expected values
        (this should really be done somewhere else, like inside a test method)
        and return an avatar appropriate for the given identifier.
        """
        assert interface == pb.IPerspective
        assert mind == "BRAINS!"
        if avatarId is checkers.ANONYMOUS:
            return pb.IPerspective, Echoer(), lambda: None
        else:
            self.lastPerspective = self.perspectiveFactory(avatarId)
            self.lastPerspective.loggedIn = True
            return (
                pb.IPerspective, self.lastPerspective,
                self.lastPerspective.logout)



class MyView(pb.Viewable):

    def view_check(self, user):
        return isinstance(user, MyPerspective)



class NewCredTestCase(unittest.TestCase):
    """
    Tests related to the L{twisted.cred} support in PB.
    """
    def setUp(self):
        """
        Create a portal with no checkers and wrap it around a simple test
        realm.  Set up a PB server on a TCP port which serves perspectives
        using that portal.
        """
        self.realm = TestRealm()
        self.portal = portal.Portal(self.realm)
        self.factory = ConnectionNotifyServerFactory(self.portal)
        self.port = reactor.listenTCP(0, self.factory, interface="127.0.0.1")
        self.portno = self.port.getHost().port


    def tearDown(self):
        """
        Shut down the TCP port created by L{setUp}.
        """
        return self.port.stopListening()


    def getFactoryAndRootObject(self, clientFactory=pb.PBClientFactory):
        """
        Create a connection to the test server.

        @param clientFactory: the factory class used to create the connection.

        @return: a tuple (C{factory}, C{deferred}), where factory is an
            instance of C{clientFactory} and C{deferred} the L{Deferred} firing
            with the PB root object.
        """
        factory = clientFactory()
        rootObjDeferred = factory.getRootObject()
        connector = reactor.connectTCP('127.0.0.1', self.portno, factory)
        self.addCleanup(connector.disconnect)
        return factory, rootObjDeferred


    def test_getRootObject(self):
        """
        Assert only that L{PBClientFactory.getRootObject}'s Deferred fires with
        a L{RemoteReference}.
        """
        factory, rootObjDeferred = self.getFactoryAndRootObject()

        def gotRootObject(rootObj):
            self.assertIsInstance(rootObj, pb.RemoteReference)
            disconnectedDeferred = Deferred()
            rootObj.notifyOnDisconnect(disconnectedDeferred.callback)
            factory.disconnect()
            return disconnectedDeferred

        return rootObjDeferred.addCallback(gotRootObject)


    def test_deadReferenceError(self):
        """
        Test that when a connection is lost, calling a method on a
        RemoteReference obtained from it raises DeadReferenceError.
        """
        factory, rootObjDeferred = self.getFactoryAndRootObject()

        def gotRootObject(rootObj):
            disconnectedDeferred = Deferred()
            rootObj.notifyOnDisconnect(disconnectedDeferred.callback)

            def lostConnection(ign):
                self.assertRaises(
                    pb.DeadReferenceError,
                    rootObj.callRemote, 'method')

            disconnectedDeferred.addCallback(lostConnection)
            factory.disconnect()
            return disconnectedDeferred

        return rootObjDeferred.addCallback(gotRootObject)


    def test_clientConnectionLost(self):
        """
        Test that if the L{reconnecting} flag is passed with a True value then
        a remote call made from a disconnection notification callback gets a
        result successfully.
        """
        class ReconnectOnce(pb.PBClientFactory):
            reconnectedAlready = False
            def clientConnectionLost(self, connector, reason):
                reconnecting = not self.reconnectedAlready
                self.reconnectedAlready = True
                if reconnecting:
                    connector.connect()
                return pb.PBClientFactory.clientConnectionLost(
                    self, connector, reason, reconnecting)

        factory, rootObjDeferred = self.getFactoryAndRootObject(ReconnectOnce)

        def gotRootObject(rootObj):
            self.assertIsInstance(rootObj, pb.RemoteReference)

            d = Deferred()
            rootObj.notifyOnDisconnect(d.callback)
            factory.disconnect()

            def disconnected(ign):
                d = factory.getRootObject()

                def gotAnotherRootObject(anotherRootObj):
                    self.assertIsInstance(anotherRootObj, pb.RemoteReference)

                    d = Deferred()
                    anotherRootObj.notifyOnDisconnect(d.callback)
                    factory.disconnect()
                    return d
                return d.addCallback(gotAnotherRootObject)
            return d.addCallback(disconnected)
        return rootObjDeferred.addCallback(gotRootObject)


    def test_immediateClose(self):
        """
        Test that if a Broker loses its connection without receiving any bytes,
        it doesn't raise any exceptions or log any errors.
        """
        serverProto = self.factory.buildProtocol(('127.0.0.1', 12345))
        serverProto.makeConnection(protocol.FileWrapper(StringIO()))
        serverProto.connectionLost(failure.Failure(main.CONNECTION_DONE))


    def test_loginConnectionRefused(self):
        """
        L{PBClientFactory.login} returns a L{Deferred} which is errbacked
        with the L{ConnectionRefusedError} if the underlying connection is
        refused.
        """
        clientFactory = pb.PBClientFactory()
        loginDeferred = clientFactory.login(
            credentials.UsernamePassword("foo", "bar"))
        clientFactory.clientConnectionFailed(
            None,
            failure.Failure(
                ConnectionRefusedError("Test simulated refused connection")))
        return self.assertFailure(loginDeferred, ConnectionRefusedError)


    def _disconnect(self, ignore, factory):
        """
        Helper method disconnecting the given client factory and returning a
        C{Deferred} that will fire when the server connection has noticed the
        disconnection.
        """
        disconnectedDeferred = Deferred()
        self.factory.protocolInstance.notifyOnDisconnect(
            lambda: disconnectedDeferred.callback(None))
        factory.disconnect()
        return disconnectedDeferred


    def test_loginLogout(self):
        """
        Test that login can be performed with IUsernamePassword credentials and
        that when the connection is dropped the avatar is logged out.
        """
        self.portal.registerChecker(
            checkers.InMemoryUsernamePasswordDatabaseDontUse(user='pass'))
        factory = pb.PBClientFactory()
        creds = credentials.UsernamePassword("user", "pass")

        # NOTE: real code probably won't need anything where we have the
        # "BRAINS!" argument, passing None is fine. We just do it here to
        # test that it is being passed. It is used to give additional info to
        # the realm to aid perspective creation, if you don't need that,
        # ignore it.
        mind = "BRAINS!"

        d = factory.login(creds, mind)
        def cbLogin(perspective):
            self.assertTrue(self.realm.lastPerspective.loggedIn)
            self.assertIsInstance(perspective, pb.RemoteReference)
            return self._disconnect(None, factory)
        d.addCallback(cbLogin)

        def cbLogout(ignored):
            self.assertTrue(self.realm.lastPerspective.loggedOut)
        d.addCallback(cbLogout)

        connector = reactor.connectTCP("127.0.0.1", self.portno, factory)
        self.addCleanup(connector.disconnect)
        return d


    def test_logoutAfterDecref(self):
        """
        If a L{RemoteReference} to an L{IPerspective} avatar is decrefed and
        there remain no other references to the avatar on the server, the
        avatar is garbage collected and the logout method called.
        """
        loggedOut = Deferred()

        class EventPerspective(pb.Avatar):
            """
            An avatar which fires a Deferred when it is logged out.
            """
            def __init__(self, avatarId):
                pass

            def logout(self):
                loggedOut.callback(None)

        self.realm.perspectiveFactory = EventPerspective

        self.portal.registerChecker(
            checkers.InMemoryUsernamePasswordDatabaseDontUse(foo='bar'))
        factory = pb.PBClientFactory()
        d = factory.login(
            credentials.UsernamePassword('foo', 'bar'), "BRAINS!")
        def cbLoggedIn(avatar):
            # Just wait for the logout to happen, as it should since the
            # reference to the avatar will shortly no longer exists.
            return loggedOut
        d.addCallback(cbLoggedIn)
        def cbLoggedOut(ignored):
            # Verify that the server broker's _localCleanup dict isn't growing
            # without bound.
            self.assertEqual(self.factory.protocolInstance._localCleanup, {})
        d.addCallback(cbLoggedOut)
        d.addCallback(self._disconnect, factory)
        connector = reactor.connectTCP("127.0.0.1", self.portno, factory)
        self.addCleanup(connector.disconnect)
        return d


    def test_concurrentLogin(self):
        """
        Two different correct login attempts can be made on the same root
        object at the same time and produce two different resulting avatars.
        """
        self.portal.registerChecker(
            checkers.InMemoryUsernamePasswordDatabaseDontUse(
                foo='bar', baz='quux'))
        factory = pb.PBClientFactory()

        firstLogin = factory.login(
            credentials.UsernamePassword('foo', 'bar'), "BRAINS!")
        secondLogin = factory.login(
            credentials.UsernamePassword('baz', 'quux'), "BRAINS!")
        d = gatherResults([firstLogin, secondLogin])
        def cbLoggedIn((first, second)):
            return gatherResults([
                    first.callRemote('getAvatarId'),
                    second.callRemote('getAvatarId')])
        d.addCallback(cbLoggedIn)
        def cbAvatarIds((first, second)):
            self.assertEqual(first, 'foo')
            self.assertEqual(second, 'baz')
        d.addCallback(cbAvatarIds)
        d.addCallback(self._disconnect, factory)

        connector = reactor.connectTCP('127.0.0.1', self.portno, factory)
        self.addCleanup(connector.disconnect)
        return d


    def test_badUsernamePasswordLogin(self):
        """
        Test that a login attempt with an invalid user or invalid password
        fails in the appropriate way.
        """
        self.portal.registerChecker(
            checkers.InMemoryUsernamePasswordDatabaseDontUse(user='pass'))
        factory = pb.PBClientFactory()

        firstLogin = factory.login(
            credentials.UsernamePassword('nosuchuser', 'pass'))
        secondLogin = factory.login(
            credentials.UsernamePassword('user', 'wrongpass'))

        self.assertFailure(firstLogin, UnauthorizedLogin)
        self.assertFailure(secondLogin, UnauthorizedLogin)
        d = gatherResults([firstLogin, secondLogin])

        def cleanup(ignore):
            errors = self.flushLoggedErrors(UnauthorizedLogin)
            self.assertEquals(len(errors), 2)
            return self._disconnect(None, factory)
        d.addCallback(cleanup)

        connector = reactor.connectTCP("127.0.0.1", self.portno, factory)
        self.addCleanup(connector.disconnect)
        return d


    def test_anonymousLogin(self):
        """
        Verify that a PB server using a portal configured with an checker which
        allows IAnonymous credentials can be logged into using IAnonymous
        credentials.
        """
        self.portal.registerChecker(checkers.AllowAnonymousAccess())
        factory = pb.PBClientFactory()
        d = factory.login(credentials.Anonymous(), "BRAINS!")

        def cbLoggedIn(perspective):
            return perspective.callRemote('echo', 123)
        d.addCallback(cbLoggedIn)

        d.addCallback(self.assertEqual, 123)

        d.addCallback(self._disconnect, factory)

        connector = reactor.connectTCP("127.0.0.1", self.portno, factory)
        self.addCleanup(connector.disconnect)
        return d


    def test_anonymousLoginNotPermitted(self):
        """
        Verify that without an anonymous checker set up, anonymous login is
        rejected.
        """
        self.portal.registerChecker(
            checkers.InMemoryUsernamePasswordDatabaseDontUse(user='pass'))
        factory = pb.PBClientFactory()
        d = factory.login(credentials.Anonymous(), "BRAINS!")
        self.assertFailure(d, UnhandledCredentials)

        def cleanup(ignore):
            errors = self.flushLoggedErrors(UnhandledCredentials)
            self.assertEquals(len(errors), 1)
            return self._disconnect(None, factory)
        d.addCallback(cleanup)

        connector = reactor.connectTCP('127.0.0.1', self.portno, factory)
        self.addCleanup(connector.disconnect)
        return d


    def test_anonymousLoginWithMultipleCheckers(self):
        """
        Like L{test_anonymousLogin} but against a portal with a checker for
        both IAnonymous and IUsernamePassword.
        """
        self.portal.registerChecker(checkers.AllowAnonymousAccess())
        self.portal.registerChecker(
            checkers.InMemoryUsernamePasswordDatabaseDontUse(user='pass'))
        factory = pb.PBClientFactory()
        d = factory.login(credentials.Anonymous(), "BRAINS!")

        def cbLogin(perspective):
            return perspective.callRemote('echo', 123)
        d.addCallback(cbLogin)

        d.addCallback(self.assertEqual, 123)

        d.addCallback(self._disconnect, factory)

        connector = reactor.connectTCP('127.0.0.1', self.portno, factory)
        self.addCleanup(connector.disconnect)
        return d


    def test_authenticatedLoginWithMultipleCheckers(self):
        """
        Like L{test_anonymousLoginWithMultipleCheckers} but check that
        username/password authentication works.
        """
        self.portal.registerChecker(checkers.AllowAnonymousAccess())
        self.portal.registerChecker(
            checkers.InMemoryUsernamePasswordDatabaseDontUse(user='pass'))
        factory = pb.PBClientFactory()
        d = factory.login(
            credentials.UsernamePassword('user', 'pass'), "BRAINS!")

        def cbLogin(perspective):
            return perspective.callRemote('add', 100, 23)
        d.addCallback(cbLogin)

        d.addCallback(self.assertEqual, 123)

        d.addCallback(self._disconnect, factory)

        connector = reactor.connectTCP('127.0.0.1', self.portno, factory)
        self.addCleanup(connector.disconnect)
        return d


    def test_view(self):
        """
        Verify that a viewpoint can be retrieved after authenticating with
        cred.
        """
        self.portal.registerChecker(
            checkers.InMemoryUsernamePasswordDatabaseDontUse(user='pass'))
        factory = pb.PBClientFactory()
        d = factory.login(
            credentials.UsernamePassword("user", "pass"), "BRAINS!")

        def cbLogin(perspective):
            return perspective.callRemote("getViewPoint")
        d.addCallback(cbLogin)

        def cbView(viewpoint):
            return viewpoint.callRemote("check")
        d.addCallback(cbView)

        d.addCallback(self.assertTrue)

        d.addCallback(self._disconnect, factory)

        connector = reactor.connectTCP("127.0.0.1", self.portno, factory)
        self.addCleanup(connector.disconnect)
        return d



class NonSubclassingPerspective:
    implements(pb.IPerspective)

    def __init__(self, avatarId):
        pass

    # IPerspective implementation
    def perspectiveMessageReceived(self, broker, message, args, kwargs):
        args = broker.unserialize(args, self)
        kwargs = broker.unserialize(kwargs, self)
        return broker.serialize((message, args, kwargs))

    # Methods required by TestRealm
    def logout(self):
        self.loggedOut = True



class NSPTestCase(unittest.TestCase):
    """
    Tests for authentication against a realm where the L{IPerspective}
    implementation is not a subclass of L{Avatar}.
    """
    def setUp(self):
        self.realm = TestRealm()
        self.realm.perspectiveFactory = NonSubclassingPerspective
        self.portal = portal.Portal(self.realm)
        self.checker = checkers.InMemoryUsernamePasswordDatabaseDontUse()
        self.checker.addUser("user", "pass")
        self.portal.registerChecker(self.checker)
        self.factory = WrappingFactory(pb.PBServerFactory(self.portal))
        self.port = reactor.listenTCP(0, self.factory, interface="127.0.0.1")
        self.addCleanup(self.port.stopListening)
        self.portno = self.port.getHost().port


    def test_NSP(self):
        """
        An L{IPerspective} implementation which does not subclass
        L{Avatar} can expose remote methods for the client to call.
        """
        factory = pb.PBClientFactory()
        d = factory.login(credentials.UsernamePassword('user', 'pass'),
                          "BRAINS!")
        reactor.connectTCP('127.0.0.1', self.portno, factory)
        d.addCallback(lambda p: p.callRemote('ANYTHING', 'here', bar='baz'))
        d.addCallback(self.assertEquals,
                      ('ANYTHING', ('here',), {'bar': 'baz'}))
        def cleanup(ignored):
            factory.disconnect()
            for p in self.factory.protocols:
                p.transport.loseConnection()
        d.addCallback(cleanup)
        return d



class IForwarded(Interface):
    """
    Interface used for testing L{util.LocalAsyncForwarder}.
    """

    def forwardMe():
        """
        Simple synchronous method.
        """

    def forwardDeferred():
        """
        Simple asynchronous method.
        """


class Forwarded:
    """
    Test implementation of L{IForwarded}.

    @ivar forwarded: set if C{forwardMe} is called.
    @type forwarded: C{bool}
    @ivar unforwarded: set if C{dontForwardMe} is called.
    @type unforwarded: C{bool}
    """
    implements(IForwarded)
    forwarded = False
    unforwarded = False

    def forwardMe(self):
        """
        Set a local flag to test afterwards.
        """
        self.forwarded = True

    def dontForwardMe(self):
        """
        Set a local flag to test afterwards. This should not be called as it's
        not in the interface.
        """
        self.unforwarded = True

    def forwardDeferred(self):
        """
        Asynchronously return C{True}.
        """
        return succeed(True)


class SpreadUtilTestCase(unittest.TestCase):
    """
    Tests for L{twisted.spread.util}.
    """

    def test_sync(self):
        """
        Call a synchronous method of a L{util.LocalAsRemote} object and check
        the result.
        """
        o = LocalRemoteTest()
        self.assertEquals(o.callRemote("add1", 2), 3)

    def test_async(self):
        """
        Call an asynchronous method of a L{util.LocalAsRemote} object and check
        the result.
        """
        o = LocalRemoteTest()
        o = LocalRemoteTest()
        d = o.callRemote("add", 2, y=4)
        self.assertIsInstance(d, Deferred)
        d.addCallback(self.assertEquals, 6)
        return d

    def test_asyncFail(self):
        """
        Test a asynchronous failure on a remote method call.
        """
        o = LocalRemoteTest()
        d = o.callRemote("fail")
        def eb(f):
            self.assertTrue(isinstance(f, failure.Failure))
            f.trap(RuntimeError)
        d.addCallbacks(lambda res: self.fail("supposed to fail"), eb)
        return d

    def test_remoteMethod(self):
        """
        Test the C{remoteMethod} facility of L{util.LocalAsRemote}.
        """
        o = LocalRemoteTest()
        m = o.remoteMethod("add1")
        self.assertEquals(m(3), 4)

    def test_localAsyncForwarder(self):
        """
        Test a call to L{util.LocalAsyncForwarder} using L{Forwarded} local
        object.
        """
        f = Forwarded()
        lf = util.LocalAsyncForwarder(f, IForwarded)
        lf.callRemote("forwardMe")
        self.assertTrue(f.forwarded)
        lf.callRemote("dontForwardMe")
        self.assertFalse(f.unforwarded)
        rr = lf.callRemote("forwardDeferred")
        l = []
        rr.addCallback(l.append)
        self.assertEqual(l[0], 1)



class PBWithSecurityOptionsTest(unittest.TestCase):
    """
    Test security customization.
    """

    def test_clientDefaultSecurityOptions(self):
        """
        By default, client broker should use C{jelly.globalSecurity} as
        security settings.
        """
        factory = pb.PBClientFactory()
        broker = factory.buildProtocol(None)
        self.assertIdentical(broker.security, jelly.globalSecurity)


    def test_serverDefaultSecurityOptions(self):
        """
        By default, server broker should use C{jelly.globalSecurity} as
        security settings.
        """
        factory = pb.PBServerFactory(Echoer())
        broker = factory.buildProtocol(None)
        self.assertIdentical(broker.security, jelly.globalSecurity)


    def test_clientSecurityCustomization(self):
        """
        Check that the security settings are passed from the client factory to
        the broker object.
        """
        security = jelly.SecurityOptions()
        factory = pb.PBClientFactory(security=security)
        broker = factory.buildProtocol(None)
        self.assertIdentical(broker.security, security)


    def test_serverSecurityCustomization(self):
        """
        Check that the security settings are passed from the server factory to
        the broker object.
        """
        security = jelly.SecurityOptions()
        factory = pb.PBServerFactory(Echoer(), security=security)
        broker = factory.buildProtocol(None)
        self.assertIdentical(broker.security, security)



class DeprecationTests(unittest.TestCase):
    """
    Tests for certain deprecations of free-functions in L{twisted.spread.pb}.
    """
    def test_noOperationDeprecated(self):
        """
        L{pb.noOperation} is deprecated.
        """
        self.callDeprecated(
            Version("twisted", 8, 2, 0),
            pb.noOperation, 1, 2, x=3, y=4)


    def test_printTraceback(self):
        """
        L{pb.printTraceback} is deprecated.
        """
        self.callDeprecated(
            Version("twisted", 8, 2, 0),
            pb.printTraceback,
            "printTraceback deprecation fake traceback value")
