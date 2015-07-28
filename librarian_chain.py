""" Chain module for use by the librarian """
from function_chain import Link, Chain
import json

class JsonLink(Link):
    """ Link that converts from dictionaries to json and vise-versa """

    def apply(self, obj):
        """ dictionary -> JSON

        Args:
            obj: Json dictionary

        Returns:
            JSON string
        """
        return json.dumps(obj)

    def unapply(self, obj):
        """ JSON -> dictionary

        Args:
            obj: JSON string

        Returns:
            Python dictionary object
        """
        return json.loads(obj)


class EncodeLink(Link):
    """ Encode and decode strings to pass to a socket """
    def apply(self, obj):
        """ Apply an encoding python string -> byte string
        Args:
            obj: Python3 string

        Returns:
            Byte string to be use with Socket.send()
        """
        return str.encode(obj)

    def unapply(self, obj):
        """ Decode encoding
        Args:
            obj: Byte string, like from Socket.recv()

        Returns:
            Python3 string
        """
        return obj.decode("utf-8")


class LibrarianChain(Chain):
    """ Chain for use with librarian code """

    @staticmethod
    def argparse_extend(parser):
        """ does nothing """
        pass

    def __init__(self, args):
        super().__init__()
        super().add_link(JsonLink())
