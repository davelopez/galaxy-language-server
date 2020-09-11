"""This module provides a service to determine position
context inside an XML document."""

from typing import Optional, List
from io import BytesIO
from pygls.workspace import Document, Position
from enum import Enum, unique
import xml.sax
import re
from .xsd.types import XsdTree, XsdNode


START_TAG_REGEX = r"<([a-z]+)[ \n]?"
ATTR_KEY_VALUE_REGEX = r" ([a-z]*)=\"([\w.]*)[\"]?"
TAG_GROUP = 1
ATTR_KEY_GROUP = 1
ATTR_VALUE_GROUP = 2


@unique
class ContextTokenType(Enum):
    """Supported types of tokens that can be found in a document."""

    UNKNOWN = 1
    TAG = 2
    ATTRIBUTE_KEY = 3
    ATTRIBUTE_VALUE = 4


class XmlContext:
    """Represents the context at a given XML document position.

    It provides information about the token under the cursor and
    the XSD node definition associated.
    """

    def __init__(
        self,
        document_line: str = "",
        position: Position = Position(),
        is_empty: bool = False,
        token_name: str = None,
        token_type: ContextTokenType = ContextTokenType.UNKNOWN,
    ):
        self.document_line: str = document_line
        self.target_position: Position = position
        self.is_empty: bool = is_empty
        self.token_name: Optional[str] = token_name
        self.token_type: ContextTokenType = token_type
        self.node: Optional[XsdNode] = None
        self.node_stack: List[str] = []

    def is_tag(self) -> bool:
        """Indicates if the token in context is a tag"""
        return self.token_type == ContextTokenType.TAG

    def is_attribute_key(self) -> bool:
        """Indicates if the token in context is an attribute key"""
        return self.token_type == ContextTokenType.ATTRIBUTE_KEY

    def is_attribute_value(self) -> bool:
        """Indicates if the token in context is an attribute value"""
        return self.token_type == ContextTokenType.ATTRIBUTE_VALUE

    def update_node_definition(self, xsd_tree: XsdTree) -> None:
        """Updates the associated node in the context using the given XSD tree definition.

        Args:
            xsd_tree (XsdTree): The XSD tree definition.
        """
        try:
            # TODO: use the whole node_stack to find the exact node definition
            last_node_in_path = self.node_stack[-1]
            self.node = xsd_tree.find_node_by_name(last_node_in_path)
        except IndexError:
            self.node = xsd_tree.root


class XmlContextService:
    """This service provides information about the XML context at
    a specific position of the document.
    """

    def __init__(self, xsd_tree: XsdTree):
        self.xsd_tree = xsd_tree

    def get_xml_context(self, document: Document, position: Position) -> XmlContext:
        """Gets the XML context at a given offset inside the document.

        Args:
            document (Document): The current document.
            position (Position): The position inside de document.

        Returns:
            XmlContext: The resulting context with the current node
            definition and other information. If the context can not be
            determined, the default context with no information is returned.
        """
        parser = XmlContextParser()
        context = parser.parse(document, position)
        context.update_node_definition(self.xsd_tree)
        return context


class XmlContextParser:
    """This class parses an XML document until it finds the token at a given
    position (line and character) and returns information about it.

    The parser will try it's best to be tolerant to imcomplete XML documents
    in order to be able to extract the token at position.
    """

    def __init__(self):
        self._document: Document = None

    def parse(self, document: Document, position: Position) -> XmlContext:
        """Parses the given document until it finds the token at the given position and
        returns context information about this token.

        Args:
            document (Document): The XML document to parse.
            position (Position): The position inside the document to get the context.

        Returns:
            XmlContext: The context information at the given position.
        """
        self._document = document

        if self.is_empty_document(document):
            return XmlContext(is_empty=True)

        context_line = document.lines[position.line]
        context = XmlContext(document_line=context_line, position=position)

        source = BytesIO(document.source.encode())
        reader = self._build_xml_reader()
        locator = xml.sax.expatreader.ExpatLocator(reader)
        reader.setContentHandler(ContextBuilderHandler(locator, context))
        reader.setErrorHandler(ContextParseErrorHandler(context))
        try:
            reader.parse(source)
        except ContextFoundException:
            pass  # All is good, we got what we needed

        return context

    def _build_xml_reader(self) -> xml.sax.xmlreader.XMLReader:
        reader = xml.sax.make_parser()
        reader.setFeature(xml.sax.handler.feature_namespaces, False)
        reader.setFeature(xml.sax.handler.feature_validation, False)
        return reader

    @staticmethod
    def is_empty_document(document: Document) -> bool:
        """Determines if the given XML document is an empty document.

        Args:
            document (Document): The document to check.

        Returns:
            bool: True if the document is empty and False otherwise.
        """
        xml_content = document.source.strip()
        return not xml_content or len(xml_content) < 2


class ContextBuilderHandler(xml.sax.ContentHandler):
    """SAX ContentHandler that tries to compose the context information using the
    different SAX events.
    """

    def __init__(self, locator, context: XmlContext):
        xml.sax.ContentHandler.__init__(self)
        self._loc = locator
        self._context: XmlContext = context
        self.setDocumentLocator(self._loc)

    def startDocument(self):
        pass

    def endDocument(self):
        pass

    def startElement(self, tag, attributes):
        self._context.node_stack.append(tag)
        current_position = self.get_current_position()

        if current_position.line == self._context.target_position.line:
            self._build_context_from_element_line(current_position.character, tag, attributes)

    def endElement(self, tag):
        self._context.node_stack.pop(-1)

    def characters(self, content):
        pass

    def get_current_position(self) -> Position:
        """Gets the current position inside the document where the parser is.

        Returns:
            Position: The current position inside the document.
        """
        return Position(line=self._loc.getLineNumber() - 1, character=self._loc.getColumnNumber())

    def _build_context_from_element_line(self, start_position: int, tag: str, attributes):
        target_offset = self._context.target_position.character
        tag_offset = start_position + len(tag)
        is_on_tag = start_position < target_offset <= tag_offset
        if is_on_tag:
            self._context.is_tag = True
            self._context.token_name = tag
            raise ContextFoundException()

        accum = tag_offset + 1  # +1 for '<'
        for attr_name, attr_value in attributes.items():
            attr_start = accum + 1  # +1 for ' '
            attr_end = attr_start + len(attr_name)
            is_on_attr = attr_start <= target_offset <= attr_end
            if is_on_attr:
                self._context.is_attribute = True
                self._context.token_name = attr_name
                raise ContextFoundException()

            attr_value_start = attr_end + 2  # +2 for '=' and '\"'
            attr_value_end = attr_value_start + len(attr_value)
            is_on_attr_value = attr_value_start <= target_offset <= attr_value_end
            if is_on_attr_value:
                self._context.is_attribute_value = True
                self._context.token_name = attr_value
                raise ContextFoundException()
            accum = attr_value_end


class ContextParseErrorHandler(xml.sax.ErrorHandler):
    """SAX ErrorHandler that tries it's best to recover context information when a fatal error
    ocurred during parsing.
    """

    def __init__(self, context: XmlContext):
        xml.sax.ErrorHandler.__init__(self)
        self._context = context

    def fatalError(self, exception: xml.sax.SAXParseException):
        position = self.get_position(exception)
        if exception.getMessage() == "unclosed token":
            self._try_process_context_from_unclosed_token(position.character)

    def get_position(self, exception: xml.sax.SAXParseException) -> Position:
        return Position(line=exception.getLineNumber() - 1, character=exception.getColumnNumber())

    def _try_process_context_from_unclosed_token(self, start_offset: int) -> None:
        target_offset = self._context.target_position.character
        self._try_get_tag_context_at_line_position(target_offset)
        self._try_get_attribute_context_at_line_position(target_offset)

    def _try_get_tag_context_at_line_position(self, target_offset):
        tag_match = re.search(START_TAG_REGEX, self._context.document_line, re.DOTALL)
        if tag_match and tag_match.start(TAG_GROUP) <= target_offset <= tag_match.end(TAG_GROUP):
            self._context.is_tag = True
            self._context.token_name = tag_match.group(TAG_GROUP)
            raise ContextFoundException()

    def _try_get_attribute_context_at_line_position(self, target_offset):
        attribute_matches = re.finditer(
            ATTR_KEY_VALUE_REGEX, self._context.document_line, re.DOTALL
        )
        if attribute_matches:
            for matchNum, match in enumerate(attribute_matches, start=1):
                if match.start(ATTR_KEY_GROUP) <= target_offset <= match.end(ATTR_KEY_GROUP):
                    self._context.is_attribute = True
                    self._context.token_name = match.group(ATTR_KEY_GROUP)
                    raise ContextFoundException()
                if match.start(ATTR_VALUE_GROUP) <= target_offset <= match.end(ATTR_VALUE_GROUP):
                    self._context.is_attribute_value = True
                    self._context.token_name = match.group(ATTR_VALUE_GROUP)
                    raise ContextFoundException()


class ContextFoundException(Exception):
    """When this exception is raised, it indicates that the necessary
    information for the context is already retrieved, so, additional
    parsing of the file can be avoided.
    """

    pass