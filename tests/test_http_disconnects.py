"""Exercise the actual HTTP handler, including its first request-line read."""
import io
import unittest
from types import SimpleNamespace as NS

import miniapp


class Reader(io.BytesIO):
    def __init__(self, payload=b"", error=None):
        super().__init__(payload)
        self.error = error

    def readline(self, limit=-1):
        if self.error:
            raise self.error
        return super().readline(limit)


class Socket:
    def __init__(self, reader, write_error=None):
        self.reader, self.write_error = reader, write_error
        self.output = bytearray()

    def makefile(self, mode, size):
        return self.reader

    def sendall(self, data):
        if self.write_error:
            raise self.write_error
        self.output.extend(data)


class DisconnectTest(unittest.TestCase):
    def setUp(self):
        self.handler = miniapp.make_handler({"pipedream":None,"github":None,"direct":None})
        self.server = NS(server_name="localhost",server_port=8080)

    def serve(self, request):
        return self.handler(request,("127.0.0.1",1234),self.server)

    def test_disconnect_before_request_does_not_escape_request_thread(self):
        for error in (ConnectionResetError(104,"reset"),ConnectionAbortedError(103,"aborted"),BrokenPipeError(32,"pipe")):
            with self.subTest(error=type(error).__name__):
                request = Socket(Reader(error=error))
                handler = self.serve(request)
                self.assertTrue(handler.close_connection)
                self.assertEqual(request.output,b"")

    def test_real_handler_still_serves_health_after_disconnected_client(self):
        self.serve(Socket(Reader(error=ConnectionResetError(104,"reset"))))
        request = Socket(Reader(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"))
        self.serve(request)
        self.assertIn(b"200 OK",request.output)
        self.assertIn(b"Secret AI is running",request.output)

    def test_disconnect_while_sending_response_is_also_contained(self):
        request = Socket(Reader(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"),
                         write_error=BrokenPipeError(32,"pipe"))
        handler = self.serve(request)
        self.assertTrue(handler.close_connection)

    def test_other_network_errors_are_not_silenced(self):
        with self.assertRaisesRegex(OSError,"other failure"):
            self.serve(Socket(Reader(error=OSError(5,"other failure"))))

    def test_handler_programming_errors_are_not_silenced(self):
        original = self.handler.do_GET
        def broken(handler):
            raise RuntimeError("broken route")
        self.handler.do_GET = broken
        try:
            with self.assertRaisesRegex(RuntimeError,"broken route"):
                self.serve(Socket(Reader(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")))
        finally:
            self.handler.do_GET = original


if __name__ == "__main__":
    unittest.main()
