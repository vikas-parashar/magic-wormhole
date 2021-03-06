import six
import requests

class EventSourceFollower:
    def __init__(self, url, timeout):
        self.resp = requests.get(url,
                                 headers={"accept": "text/event-stream"},
                                 stream=True,
                                 timeout=timeout)
        self.resp.raise_for_status()

    def close(self):
        self.resp.close()

    def _get_fields(self, lines):
        while True:
            first_line = next(lines) # raises StopIteration when closed
            assert isinstance(first_line, type(six.u(""))), type(first_line)
            fieldname, data = first_line.split(": ", 1)
            data_lines = [data]
            while True:
                next_line = next(lines)
                if not next_line: # empty string, original was "\n"
                    yield (fieldname, "\n".join(data_lines))
                    break
                data_lines.append(next_line)

    def iter_events(self):
        # I think Request.iter_lines and .iter_content use chunk_size= in a
        # funny way, and nothing happens until at least that much data has
        # arrived. So unless we set chunk_size=1, we won't hear about lines
        # for a long time. I'd prefer that chunk_size behaved like
        # read(size), and gave you 1<=x<=size bytes in response.
        eventtype = "message"
        lines_iter = self.resp.iter_lines(chunk_size=1, decode_unicode=True)
        for (fieldname, data) in self._get_fields(lines_iter):
            # fieldname/data are unicode on both py2 and py3. On py2, where
            # ("abc"==u"abc" is True), this compares unicode against str,
            # which matches. On py3, where (b"abc"=="abc" is False), this
            # compares unicode against unicode, which matches.
            if fieldname == "data":
                yield (eventtype, data)
                eventtype = "message"
            elif fieldname == "event":
                eventtype = data
            else:
                print("weird fieldname", fieldname, type(fieldname), data)
