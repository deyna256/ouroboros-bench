"""Ouroboros with the mid profile; see ouroboros_convy/launch.py for what it sets."""

from ouroboros_convy import Ouroboros, Server

server = Server("mid")
agent = Ouroboros(server)
version = server.version()
