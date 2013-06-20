#! /usr/bin/env python3

# 
# Cpppo -- Communication Protocol Python Parser and Originator
# 
# Copyright (c) 2013, Hard Consulting Corporation.
# 
# Cpppo is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.  See the LICENSE file at the top of the source tree.
# 
# Cpppo is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
# 

from __future__ import absolute_import
from __future__ import print_function

__author__                      = "Perry Kundert"
__email__                       = "perry@hardconsulting.com"
__copyright__                   = "Copyright (c) 2013 Hard Consulting Corporation"
__license__                     = "Dual License: GPLv3 (or later) and Commercial (see LICENSE)"


"""
enip.device	-- support for implementing an EtherNet/IP device Objects and Attributes

"""
__all__				= ['lookup', 'resolve', 'resolve_element',
                                   'redirect_tag', 'resolve_tag', 
                                   'Object', 'Attribute',
                                   'UCMM', 'Connection_Manager', 'Message_Router', 'Identity']

import array
import codecs
import errno
import logging
import os
import random
import sys
import threading
import time
import traceback
try:
    import reprlib
except ImportError:
    import repr as reprlib

import cpppo
from   cpppo import misc
import cpppo.server
from   cpppo.server import network

from .parser import *

if __name__ == "__main__":
    logging.basicConfig( **cpppo.log_cfg )
    #logging.getLogger().setLevel( logging.DETAIL )

log				= logging.getLogger( "enip.dev" )

# 
# directory	-- All available device Objects and Attributes (including the "class" instance 0)
# lookup	-- Find a class/instance/attribute
# 
#     Object/Instance/Attribute lookup.  The Object is stored at (invalid)
# attribute_id 0.   For example:
# 
#         directory.6.0		Class 6, Instance 0: (metaclass) directory of Object/Attributes
#         directory.6.1		Class 6, Instance 1: (instance)  directory of Object/Attributes
#         directory.6.1.0	Class 6, Instance 1: device.Object (python instance)
#         directory.6.1.1	Class 6, Instance 1, Attribute 1 device.Attribute (python instance)
# 
directory			= cpppo.dotdict()

def __directory_path( class_id, instance_id=0, attribute_id=None ):
    """It is not possible to in produce a path with an attribute_id=0; this is
    not a invalid Attribute ID.  The '0' entry is reserved for the Object, which is
    only accessible with attribute_id=None."""
    assert attribute_id != 0, \
        "Class %5d/0x%04x, Instance %3d; Invalid Attribute ID 0"
    return str( class_id ) \
        + '.' + str( instance_id ) \
        + '.' + ( str( attribute_id if attribute_id else 0 ))

def lookup( class_id, instance_id=0, attribute_id=None ):
    """Lookup by path ("#.#.#" string type), or class/instance/attribute ID, or """
    exception			= None
    try:
        key			= class_id
        if not isinstance( class_id, cpppo.type_str_base ):
            assert type( class_id ) is int
            key			= __directory_path(
                class_id=class_id, instance_id=instance_id, attribute_id=attribute_id )
        res			= directory.get( key, None )
    except Exception as exc:
        exception		= exc
        res			= None
    finally:
        log.detail( "Class %5d/0x%04x, Instance %3d, Attribute %5r ==> %s",
                    class_id, class_id, instance_id, attribute_id, 
                    res if not exception else ( "Failed: %s" % exception ))
    return res

# 
# symbol	-- All known symbolic address
# redirect_tag	-- Direct a tag to a class, instance and attribute
# resolve*	-- Resolve the class, instance [and attribute] from a path or tag.
# 
# A path is something of the form:
# 
#     {
#         'size':6,
#         'segment':[
#             {'symbolic':'SCADA'}, 
#             {'element':123}]
#     }
# 
# Multiple symbolic and element entries are allowed.  This is used for addressing structures:
# 
#     boo[3].foo
# 
# or for indexing multi-dimensional arrays:
# 
#     table[3][4]
# 
# or returning arbitrary sets of elements from an array:
# 
#     array[3,56,179]
# 
# The initial segments of the path must address a class and instance.
# 
symbol				= {}
symbol_keys			= ('class', 'instance', 'attribute')

def redirect_tag( tag, address ):
    """Establish (or change) a tag, redirecting it to the specified class/instance/attribute address.
    Make sure we stay with only str type tags (mostly for Python2, in case somehow we get a Unicode
    tag)"""
    tag				= str( tag )
    assert isinstance( address, dict )
    assert all( k in symbol_keys for k in address )
    assert all( k in address     for k in symbol_keys )
    symbol[tag]			= address

def resolve_tag( tag ):
    """Return the (class_id, instance_id, attribute_id) tuple corresponding to tag, or None if not specified"""
    address			= symbol.get( str( tag ), None )
    if address:
        return tuple( address[k] for k in symbol_keys )
    return None


def resolve( path, attribute=False ):
    """Given a path, returns the fully resolved (class,instance[,attribute]) tuple required to lookup an
    Object/Attribute.  Won't allow over-writing existing elements (eg. 'class') with symbolic data
    results.  Call with attribute=True to force resolving to the Attribute level; otherwise, always
    returns None for the attribute.

    """

    result			= { 'class': None, 'instance': None, 'attribute': None }

    for term in path['segment']:
        if ( result['class'] is not None and result['instance'] is not None
             and ( not attribute or result['attribute'] is not None )):
            break # All desired terms specified; done!
        working		= dict( term )
        while working:
            # Each term is something like {'class':5}, {'instance':1}, or (from symbol table):
            # {'class':5,'instance':1}.  Pull each key (eg. 'class') from working into result,
            # but only if 
            for key in result:
                if key in working:
                    assert result[key] is None, \
                        "Failed to override %r==%r with %r from path segment %r in path %r" % (
                            key, result[key], working[key], term, path['segment'] )
                    result[key] = working.pop( key )
            if working:
                assert 'symbolic' in working, \
                    "Invalid term %r found in path %r" % ( working, path['segment'] )
                sym	= str( working['symbolic'] )
                assert sym in symbol, \
                    "Unrecognized symbolic name %r found in path %r" % ( sym, path['segment'] )
                working	= dict( symbol[sym] )

    assert ( result['class'] is not None and result['instance'] is not None
             and ( not attribute or result['attribute'] is not None )), \
        "Failed to resolve required Class (%r), Instance (%r) %s Attribute(%r) from path: %r" % (
            result['class'], result['instance'], "and the" if attribute else "but not",
            result['attribute'], path['segment'] )
    result		= result['class'], result['instance'], result['attribute'] if attribute else None
    log.detail( "Class %5d/0x%04x, Instance %3d, Attribute %5r <== %r",
                result[0], result[0], result[1], result[2], path['segment'] )

    return result

def resolve_element( path ):
    """Resolve an element index tuple from the path; defaults to (0, ) (the 0th element of a
    single-dimensional array).

    """
    element		= []
    for term in path['segment']:
        if 'element' in term:
            element.append( term['element'] )
            break
    return tuple( element ) if element else (0, ) 

# 
# EtherNet/IP CIP Object Attribute
# 
class Attribute( object ):
    """A simple Attribute just has a default scalar value of 0.  We'll instantiate an instance of the
    supplied enip.TYPE/STRUCT class as the Attribute's .parser property.  This can be used to parse
    incoming data, and produce the current value in bytes form.
    
    The value defaults to a scalar 0, but may be configured as an array by setting default to a list
    of values of the desired array size.

    If an error code is supplied, requests on the Attribute should fail with that code.
    """
    def __init__( self, name, type_cls, default=0, error=0x00 ):
        self.name		= name
        self.default	       	= default
        self.parser		= type_cls()
        self.error		= error		# If an error code is desired on access

    def __str__( self ):
        value			= self.value
        return "%-12s %5s[%4d] == %s" % (
            self.name, self.parser.__class__.__name__, len( self ), reprlib.repr( self.value ))
    __repr__ 			= __str__

    def __len__( self ):
        """Scalars are limited to 1 indexable element, while arrays (implemented as lists) are limited to
        their length. """
        return 1 if not isinstance( self.value, list ) else len( self.value )

    @property
    def value( self ):
        return self.default

    # Indexing.  This allows us to get/set individual values in the Attribute's underlying data repository.
    def __getitem__( self, key ):
        if not isinstance( key, int ) or key >= len( self ):
            raise KeyError( "Attempt to get item at key %r beyond attribute length %d" % ( key, len( self )))
        if isinstance( self.default, list ):
            return self.default[key]
        else:
            return self.default

    def __setitem__( self, key, value ):
        """Allow setting a scalar or indexable array item."""
        if not isinstance( key, int ) or key >= len( self ):
            raise KeyError( "Attempt to set item at key %r beyond attribute length %d" % ( key, len( self )))
        if isinstance( self.default, list ):
            self.default[key] 	= value
        else:
            self.default	= value

    def produce( self, start=0, stop=None ):
        """Output the binary rendering of the current value, using enip type_cls instance configured, to
        produce the value in binary form ('produce' is normally a classmethod on the type_cls)."""
        if isinstance( self.value, list ):
            # Vector
            if stop is None:
                stop		= len( self.value )
            return b''.join( self.parser.produce( v ) for v in self.value[start:stop] )
        # Scalar
        return self.parser.produce( self.value )
        

class MaxInstance( Attribute ):
    def __init__( self, name, type_cls, class_id=None, **kwds ):
        assert class_id is not None
        self.class_id		= class_id
        super( MaxInstance, self ).__init__( name=name, type_cls=type_cls, **kwds )

    @property
    def value( self ):
        """Look up any instance of the specified class_id; it has a max_instance property, which
        is the maximum instance number allocated thus far. """
        return lookup( self.class_id, 0 ).max_instance

    def __setitem__( self, key, value ):
        raise AssertionError("Cannot set value")


class NumInstances( MaxInstance ):
    def __init__( self, name, type_cls, **kwds ):
        super( NumInstances, self ).__init__( name=name, type_cls=type_cls, **kwds )

    @property
    def value( self ):
        """Count how many instances are presently in existence; use the parent class MaxInstances.value."""
        return sum( lookup( class_id=self.class_id, instance_id=i_id ) is not None
                    for i_id in range( 1, super( NumInstances, self ).value + 1 ))

    def __setitem__( self, key, value ):
        raise AssertionError("Cannot set value")

# 
# EtherNet/IP CIP Object
# 
# Some of the standard objects (Vol 1-3.13, Table 5-1-1):
# 
#     Class Code	Object
#     ----------	------
#     0x01		Identity
#     0x02		Message Router
#     0x03		DeviceNet
#     0x04		Assembly
#     0x05 		Connection
#     0x06		Connection Manager
#     0x07		Register
# 
# Figure 1-4.1 CIP Device Object Model
#                                                       +-------------+
#   Unconnected        -------------------------------->| Unconnected |
#   Explicit Messages  <--------------------------------| Message     |
#                                                       | Manager     |           
#                                                       +-------------+
#                                                            |^            
#                                                            ||           
#                                                            ||          +-------------+
#                                                            ||          | Link        |
#                                                            ||          | Specific    |
#                                                            ||          | Objects     |
#                                                            ||          +-------------+
#                                                            v|              ^v
#                                                       +-------------+      ||               
#   Connection         -->       Explcit                | Message     |      ||
#   Based              <--       Messaging      <--     | Router      |>-----+|                 
#   Explicit                     Connection     -->     |             |<------+                 
#   Message                      Objects                +-------------+                 
#                                                            |^                          
#                                                            ||                          
#                                                            ||                                                    
#                                                            ||                                                    
#                                                            v|                                                    
#                                                       +-------------+                               
#   I/O                -->       I/O       ..+          | Application |                               
#   Messages           <--       Connection  v  <..     | Objects     |                               
#                                Objects   ..+  -->     |             |                               
#                                                       +-------------+                               
#                                                                                      
#                                                                                      
#                                                                                      
class Object( object ):
    """An EtherNet/IP device.Object is capable of parsing and processing a number of requests.  It has
    a class_id and an instance_id; an instance_id of 0 indicates the "class" instance of the
    device.Object, which has different (class level) Attributes (and may respond to different commands)
    than the other instance_id's.

    Each Object has a single class-level parser, which is used to register all of its available
    service request parsers.  The next available symbol designates the type of service request,
    eg. 0x01 ==> Get Attributes All.  These parsers enumerate the requests that are *possible* on
    the Object.  Later, when the Object is requested to actually process the request, a decision can
    be made about whether the request is *allowed*.

    The parser knows how to parse any requests it must handle, and any replies it can generate, and
    puts the results into the provided data artifact.

    Assuming Obj is an instance of Object, and the source iterator produces the incoming symbols:

        0x52, 0x04, 0x91, 0x05, 0x53, 0x43, 0x41, 0x44, #/* R...SCAD */
        0x41, 0x00, 0x14, 0x00, 0x02, 0x00, 0x00, 0x00, #/* A....... */

    then we could run the parser:

        data = cpppo.dotdict()
        with Obj.parse as machine:
            for m,w in machine.run( source=source, data=data ):
                pass
    
    and it would parse a recognized command (or reply, but that would be unexpected), and produce
    the following entries (in data, under the current context):

            'service': 			0x52,
            'path.segment': 		[{'symbolic': 'SCADA', 'length': 5}],
            'read_frag.elements':	20,
            'read_frag.offset':		2,

    Then, we could process the request:

        proceed = Obj.request( data )

    and this would process a request, converting it into a reply (any data elements unchanged by the
    reply remain):

            'service': 			0xd2,			# changed: |= 0x80
            'status':			0x00,			# default if not specified
            'path.segment': 		[{'symbolic': 'SCADA', 'length': 5}], # unchanged
            'read_frag.elements':	20,			# unchanged
            'read_frag.offset':		2,			# unchanged
            'read_frag.type':		0x00c3,			# produced for reply
            'read_frag.data':	[				# produced for response
                0x104c, 0x0008,
                0x0003, 0x0002, 0x0002, 0x0002,
                0x000e, 0x0000, 0x0000, 0x42e6,
                0x0007, 0x40c8, 0x40c8, 0x0000,
                0x00e4, 0x0000, 0x0064, 0x02b2,
                0x80c8
            ]
            'input':			bytearray( [	# encoded response payload
                                                        0xd2, 0x00, #/* ....,... */
                    0x00, 0x00, 0xc3, 0x00, 0x4c, 0x10, 0x08, 0x00, #/* ....L... */
                    0x03, 0x00, 0x02, 0x00, 0x02, 0x00, 0x02, 0x00, #/* ........ */
                    0x0e, 0x00, 0x00, 0x00, 0x00, 0x00, 0xe6, 0x42, #/* .......B */
                    0x07, 0x00, 0xc8, 0x40, 0xc8, 0x40, 0x00, 0x00, #/* ...@.@.. */
                    0xe4, 0x00, 0x00, 0x00, 0x64, 0x00, 0xb2, 0x02, #/* ....d... */
                    0xc8, 0x80                                      #/* .@ */
                ]

    The response payload is also produced as a bytes array in data.input, encoded and ready for
    transmission, or encapsulation by the next higher level of request processor (eg. a
    Message_Router, encapsulating the response into an EtherNet/IP response).

    """
    max_instance		= 0
    lock			= threading.Lock()
    service			= {} # Service number/name mappings
    transit			= {} # Symbol to transition to service parser on

    # The parser doesn't add a layer of context; run it with a path= keyword to add a layer
    parser			= cpppo.dfa( service, initial=cpppo.state( 'select' ),
                                             terminal=True )

    @classmethod
    def register_service_parser( cls, number, name, short, machine ):
        """Registers a parser with the Object. """

        log.detail( "%s Registers Service 0x%02x --> %s ", cls.__name__, number, name )
        assert number not in cls.service and name not in cls.service, \
            "Duplicate service #%d: %r registered for Object %s" % ( number, name, cls.__name__ )

        cls.service[number]	= name
        cls.service[name]	= number
        cls.transit[number]	= chr( number ) if sys.version_info.major < 3 else number
        cls.parser.initial[cls.transit[number]] \
				= cpppo.dfa( name=short, initial=machine, terminal=True )

    
    GA_ALL_NAM			= "Get Attributes All"
    GA_ALL_CTX			= "get_attributes_all"
    GA_ALL_REQ			= 0x01
    GA_ALL_RPY			= GA_ALL_REQ | 0x80
    GA_SNG_NAM			= "Get Attribute Single"
    GA_SNG_REQ			= 0x0e
    GA_SNG_RPY			= GA_SNG_REQ | 0x80
    SA_SNG_NAM			= "Set Attribute Single"
    SA_SNG_REQ			= 0x10
    SA_SNG_RPY			= SA_SNG_REQ | 0x80

    def __init__( self, name=None, instance_id=None ):
        """Create the instance (default to the next available instance_id).  An instance_id of 0 holds
        the "class" attributes/commands.

        """
        self.name		= name or self.__class__.__name__

        # Allocate and/or keep track of maximum instance ID assigned thus far.
        if instance_id is None:
            instance_id		= self.__class__.max_instance + 1
        if instance_id > self.__class__.max_instance:
            self.__class__.max_instance = instance_id
        self.instance_id	= instance_id

        ( log.normal if self.instance_id else log.info )( 
            "%24s, Class ID 0x%04x, Instance ID %3d created",
            self, self.class_id, self.instance_id )

        instance		= lookup( self.class_id, instance_id )
        assert instance is None, \
            "CIP Object class %x, instance %x already exists" % ( self.class_id, self.instance_id )

        # 
        # directory.1.2.None 	== self
        # self.attribute 	== directory.1.2 (a dotdict), for direct access of our attributes
        # 
        self.attribute		= directory.setdefault( str( self.class_id )+'.'+str( instance_id ),
                                                        cpppo.dotdict() )
        self.attribute['0']	= self

        # Check that the class-level instance (0) has been created; if not, we'll create one using
        # the default parameters.  If this isn't appropriate, then the user should create it using
        # the appropriate parameters.
        if lookup( self.class_id, 0 ) is None:
            self.__class__( name='meta-'+self.name, instance_id=0 )

        if self.instance_id == 0:
            # Set up the default Class-level values.
            self.attribute['1']= Attribute( 	'Revision', 		INT, default=0 )
            self.attribute['2']= MaxInstance( 'Max Instance',		INT,
                                                class_id=self.class_id )
            self.attribute['3']= NumInstances( 'Num Instances',		INT,
                                                class_id=self.class_id )
            # A UINT array; 1st UINT is size (default 0)
            self.attribute['4']= Attribute( 	'Optional Attributes',	INT, default=0 )
            

    def __str__( self ):
        return self.name
    
    def __repr__( self ):
        return "(0x%02x,%3d) %s" % ( self.class_id, self.instance_id, self )

    def request( self, data ):
        """Handle a request, converting it into a response.  Must be a dotdict data artifact such as is
        produced by the Object's parser.  For example, a request data containing either of the
        following:

            {
                'service':		0x01,
                'get_attributes_all':	True,
            }

        should run the Get Attribute All service, and return True if the channel should continue.
        In addition, we produce the bytes used by any higher level encapsulation.

        TODO: Validate the request.
        """
        result			= b''

        log.detail( "%s Request: %s", self, enip_format( data ))
        try:
            # Validate the request.  As we process, ensure that .status is set to reflect the
            # failure mode, should an exception be raised.  Return True iff the communications
            # channel should continue.
            data.status		= 0x08		# Service not supported, if not recognized
            data.pop( 'status_ext', None )

            if ( data.get( 'service' ) == self.GA_ALL_REQ
                 or 'get_attributes_all' in data and data.setdefault( 'service', self.GA_ALL_REQ ) == self.GA_ALL_REQ ):
                pass
            else:
                raise AssertionError( "Unrecognized Service Request" )

            # A recognized request; process the request data artifact, converting it into a reply.
            data.service           |= 0x80
                
            if data.service == self.GA_ALL_RPY:
                # Get Attributes All.  Collect up the bytes representing the attributes.  Replace
                # the place-holder .get_attribute_all=True with a real dotdict.
                data.status	= 0x08 # Service not supported, if we fail to access an Attribute
                result		= b''
                a_id		= 1
                while str(a_id) in self.attribute:
                    result     += self.attribute[str(a_id)].produce()
                    a_id       += 1
                data.get_attributes_all = cpppo.dotdict()
                data.get_attributes_all.data = bytearray( result )

                data.status	= 0x00
                data.pop( 'status_ext', None )

                # TODO: Other request processing here... 
            else:
                raise AssertionError( "Unrecognized Service Reply" )
        except Exception as exc:
            log.warning( "%r Service 0x%02x %s failed with Exception: %s\nRequest: %s\n%s\nStack %s", self,
                         data.service if 'service' in data else 0,
                         ( self.service[data.service]
                           if 'service' in data and data.service in self.service
                           else "(Unknown)" ), exc, enip_format( data ),
                         ''.join( traceback.format_exception( *sys.exc_info() )),
                         ''.join( traceback.format_stack()))

            assert data.status != 0x00, \
                "Implementation error: must specify .status error code before raising Exception"
            pass

        # Always produce a response payload; if a failure occurred, will contain an error status.
        # If this fails, we'll raise an exception for higher level encapsulation to handle.
        data.input		= bytearray( self.produce( data ))
        log.detail( "%s Response: %s: %s", self, self.service[data.service], enip_format( data ))
        return True # We shouldn't be able to terminate a connection at this level

    @classmethod
    def produce( cls, data ):
        result			= b''
        if ( data.get( 'service' ) == cls.GA_ALL_REQ 
             or 'get_attributes_all' in data and data.setdefault( 'service', cls.GA_ALL_REQ ) == cls.GA_ALL_REQ ):
            # Get Attributes All
            result	       += USINT.produce(	data.service )
            result	       += EPATH.produce(	data.path )
        elif data.get( 'service' ) == cls.GA_ALL_RPY:
            # Get Attributes All Reply
            result	       += USINT.produce(	data.service )
            result	       += b'\x00' # reserved
            result	       += status.produce( 	data )
            result	       += octets_encode( 	data.get_attributes_all.data )
        else:
            assert False, "%s doesn't recognize request/reply format: %r" % ( cls.__name__, data )
        return result

# Register the standard Object parsers
def __get_attributes_all():
    srvc			= USINT(		 	context='service' )
    srvc[True]		= path	= EPATH(			context='path')
    path[None]		= mark	= octets_noop(			context='get_attributes_all',
                                                terminal=True )
    mark.initial[None]		= move_if( 	'mark',		initializer=True )
    return srvc

Object.register_service_parser( number=Object.GA_ALL_REQ, name=Object.GA_ALL_NAM, 
                                short=Object.GA_ALL_CTX, machine=__get_attributes_all() )

def __get_attributes_all_reply():
    srvc			= USINT(		 	context='service' )
    srvc[True]	 	= rsvd	= octets_drop(	'reserved',	repeat=1 )
    rsvd[True]		= stts	= status()
    stts[None]		= data	= octets(			context='get_attributes_all',
                                                octets_extension='.data',
                                            	terminal=True )
    data[True]		= data	# Soak up all remaining data

    return srvc

Object.register_service_parser( number=Object.GA_ALL_RPY, name=Object.GA_ALL_NAM + " Reply", 
                                short=Object.GA_ALL_CTX, machine=__get_attributes_all_reply() )



class Identity( Object ):
    class_id			= 0x01

    def __init__( self, name=None, **kwds ):
        super( Identity, self ).__init__( name=name, **kwds )

        if self.instance_id == 0:
            # Extra Class-level Attributes
            pass
        else:
            # Instance Attributes (these example defaults are from a Rockwell Logix PLC)
            self.attribute['1']	= Attribute( 'Vendor Number', 		INT,	default=0x0001 )
            self.attribute['2']	= Attribute( 'Device Type', 		INT,	default=0x000e )
            self.attribute['3']	= Attribute( 'Product Code Number',	INT,	default=0x0036 )
            self.attribute['4']	= Attribute( 'Product Revision', 	INT,	default=0x0b14 )
            self.attribute['5']	= Attribute( 'Status Word', 		INT,	default=0x3160 )
            self.attribute['6']	= Attribute( 'Serial Number', 		DINT,	default=0x006c061a )
            self.attribute['7']	= Attribute( 'Product Name', 		SSTRING,default='1756-L61/B LOGIX5561' )


class UCMM( Object ):
    """Un-Connected Message Manager, handling Register/Unregister of connections, and sending
    Unconnected Send messages to either directly to a local object, or to the local Connection
    Manager for parsing/processing.


    Forwards encapsulated messages to their destination port and link address, and returns the
    encapsulated response.  The Unconnected Send message contains an encapsulated message and a
    route path with 1 or more route segment groups.  If more than 1 group remains, the first group
    is removed, and the address is used to establish a connection and send the message on; the
    response is received and returned.

    When only the final route path segment remains, the encapsulated message is sent to the local
    Message Router, and its response is received and returned.

    Presently, we only respond to Unconnected Send messages with one route path segment; a local
    port/link address.

    """

    class_id			= 0x9999	# Not an addressable Object

    parser			= CIP()
    command			= {
        0x0065: "Register Session",
        0x0066: "Unregister Session",
        0x006f: "SendRRData",
    }
    lock			= threading.Lock()
    sessions			= {}		# All known session handles, by addr


    def request( self, data ):
        """Handles a parsed enip.* request, and converts it into an appropriate response.  For
        connection related requests (Register, Unregister), handle locally.  Return True iff request
        processed and connection should proceed to process further messages.

        """
        log.detail( "%r Request: %s", self, enip_format( data ))

        proceed			= True

        assert 'addr' in data, "Connection Manager requires client address"

        # Each EtherNet/IP enip.command expects an appropriate encapsulated response
        if 'enip' in data:
            data.enip.pop( 'input', None )
        try:
            if 'enip.CIP.register' in data:
                # Allocates a new session_handle, and returns the register.protocol_version and
                # .options_flags unchanged (if supported)
        
                with self.lock:
                    session	= random.randint( 0, 2**32 )
                    while not session or session in self.__class__.sessions:
                        session	= random.randint( 0, 2**32 )
                    self.__class__.sessions[data.addr] = session
                data.enip.session_handle = session
                log.normal( "EtherNet/IP (Client %r) Session Established: %r", data.addr, session )
                data.enip.input	= bytearray( self.parser.produce( data.enip ))
                data.enip.status= 0x00

            elif 'enip.CIP.unregister' in data or 'enip' not in data:
                # Session being closed.  There is no response for this command; return False
                # inhibits any EtherNet/IP response from being generated, and closes connection.
                with self.lock:
                    session	= self.__class__.sessions.pop( data.addr, None )
                log.normal( "EtherNet/IP (Client %r) Session Terminated: %r", data.addr, 
                            session or "(Unknown)" )
                proceed		= False
            
            elif 'enip.CIP.send_data' in data:
                # An Unconnected Send (SendRRData) message may be to a local object, eg:
                # 
                #     "enip.CIP.send_data.CPF.count": 2, 
                #     "enip.CIP.send_data.CPF.item[0].length": 0, 
                #     "enip.CIP.send_data.CPF.item[0].type_id": 0, 
                #     "enip.CIP.send_data.CPF.item[1].length": 6, 
                #     "enip.CIP.send_data.CPF.item[1].type_id": 178, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.path.segment[0].class": 102, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.path.segment[1].instance": 1, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.path.size": 2, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.service": 1, 
                #     "enip.CIP.send_data.interface": 0, 
                #     "enip.CIP.send_data.timeout": 5, 
                
                # via the Message Router (note the lack of ...unconnected_send.route_path), or
                # potentially to a remote object, via the backplane or a network link route path:

		#     "enip.CIP.send_data.CPF.count": 2, 
		#     "enip.CIP.send_data.CPF.item[0].length": 0, 
		#     "enip.CIP.send_data.CPF.item[0].type_id": 0, 
		#     "enip.CIP.send_data.CPF.item[1].length": 20, 
		#     "enip.CIP.send_data.CPF.item[1].type_id": 178, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.length": 6, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.priority": 1, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.input": "array('c', '\\x01\\x02 \\x01$\\x01')", 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.path.segment[0].class": 6, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.path.segment[1].instance": 1, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.path.size": 2, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.route_path.segment[0].link": 0, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.route_path.segment[0].port": 1, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.route_path.size": 1, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.service": 82, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.timeout_ticks": 250, 
                # which carries:
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.get_attributes_all": true, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.path.segment[0].class": 1, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.path.segment[1].instance": 1, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.path.size": 2, 
		#     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.service": 1, 
                # or:
                #     "enip.CIP.send_data.CPF.count": 2, 
                #     "enip.CIP.send_data.CPF.item[0].length": 0, 
                #     "enip.CIP.send_data.CPF.item[0].type_id": 0, 
                #     "enip.CIP.send_data.CPF.item[1].length": 32, 
                #     "enip.CIP.send_data.CPF.item[1].type_id": 178, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.length": 18, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.priority": 5, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.input": "array('c', 'R\\x05\\x91\\x05SCADA\\x00(\\x0c\\x01\\x00\\x00\\x00\\x00\\x00')", 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.path.segment[0].class": 6, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.path.segment[1].instance": 1, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.path.size": 2, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.route_path.segment[0].link": 0, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.route_path.segment[0].port": 1, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.route_path.size": 1, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.service": 82, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.timeout_ticks": 157, 
                #     "enip.CIP.send_data.interface": 0, 
                #     "enip.CIP.send_data.timeout": 5,
                # which encapsulates:
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.path.segment[0].symbolic": "SCADA", 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.path.segment[1].element": 12, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.path.size": 5, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.read_frag.elements": 1, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.read_frag.offs et": 0, 
                #     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.service": 82, 

                # which must (also) be processed by the Message Router at the end of all the address
                # or backplane hops.

                # In this implementation, we can *only* process un-routed requests, or requests
                # routed to the local backplane: port 1, link 0.  All Unconnected Requests have a
                # NULL Address in CPF item 0.
                assert 'enip.CIP.send_data.CPF' in data \
                    and data.enip.CIP.send_data.CPF.count == 2 \
                    and data.enip.CIP.send_data.CPF.item[0].length == 0, \
                    "EtherNet/IP UCMM remote routed requests unimplemented"
                unc_send		= data.enip.CIP.send_data.CPF.item[1].unconnected_send
                if 'path' in unc_send:
                    ids			= resolve( unc_send.path )
                    assert ids[0] == 0x06 and ids[1] == 1, \
                        "Unconnected Send targeted Object other than Connection Manager: 0x%04x/%d" % ( ids[0], ids[1] )
                if 'route_path.segment' in unc_send:
                    assert len( unc_send.route_path.segment ) == 1 \
                        and unc_send.route_path.segment[0] == {'link': 0, 'port':1}, \
                        "Unconnected Send routed to link other than backplane link 1, port 0: %r" % unc_send.route_path
                CM			= lookup( class_id=0x06, instance_id=1 )
                CM.request( unc_send )
                
                # After successful processing of the Unconnected Send on the target node, we
                # eliminate the Unconnected Send wrapper (the unconnected_send.service = 0x52,
                # route_path, etc), and replace it with a simple encapsulated raw request.input.  We
                # do that by emptying out the unconnected_send, except for the bare request.
                # Basically, all the Unconnected Send encapsulation and routing is used to deliver
                # the request to the target Object, and then is discarded and the EtherNet/IP
                # envelope is simply returned directly to the originator carrying the response
                # payload.
                log.detail( "%s Repackaged: %s", self, enip_format( data ))
                
                data.enip.CIP.send_data.CPF.item[1].unconnected_send  = cpppo.dotdict()
                data.enip.CIP.send_data.CPF.item[1].unconnected_send.request = unc_send.request

                # And finally, re-encapsulate the CIP SendRRData, with its (now unwrapped)
                # Unconnected Send request response payload.
                log.detail( "%s Regenerating: %s", self, enip_format( data ))
                data.enip.input		= bytearray( self.parser.produce( data.enip ))
                
        except Exception as exc:
            # On Exception, if we haven't specified a more detailed error code, return Service not
            # supported.  This 
            log.warning( "%r Command 0x%04x %s failed with Exception: %s\nRequest: %s\n%s", self,
                         data.enip.command if 'enip.command' in data else 0,
                         ( self.command[data.enip.command]
                           if 'enip.command' in data and data.enip.command in self.command
                           else "(Unknown)"), exc, enip_format( data ),
                         ''.join( traceback.format_exception( *sys.exc_info() )))
            if 'enip.status' not in data or data.enip.status == 0x00:
                data['enip.status']	= 0x08 # Service not supported
            pass


        # The enip.input EtherNet/IP encapsulation is assumed to have been filled in.  Otherwise, no
        # encapsulated response is expected.
        log.detail( "%s Response: %s", self, enip_format( data ))
        return proceed
            

class Message_Router( Object ):
    """Processes incoming requests.  Normally a derived class would expand the normal set of Services
    with any specific to the actual device.

    """
    class_id			= 0x02


class Connection_Manager( Object ):
    """The Connection Manager (Class 0x06, Instance 1) Handles Unconnected Send (0x82) requests, such as:

        "unconnected_send.service": 82, 
        "unconnected_send.path.size": 2, 
        "unconnected_send.path.segment[0].class": 6, 
        "unconnected_send.path.segment[1].instance": 1, 
        "unconnected_send.priority": 5, 
        "unconnected_send.timeout_ticks": 157
        "unconnected_send.length": 16, 
        "unconnected_send.request.input": "array('B', [82, 4, 145, 5, 83, 67, 65, 68, 65, 0, 20, 0, 2, 0, 0, 0])", 
        "unconnected_send.route_path.octets.input": "array('B', [1, 0, 1, 0])", 

    If the message contains an request (.length > 0), we get the Message Router (Class 0x02,
    Instance 1) to parse and process the request, eg:

        "unconnected_send.request.service": 82, 
        "unconnected_send.request.path.size": 4, 
        "unconnected_send.request.path.segment[0].length": 5, 
        "unconnected_send.request.path.segment[0].symbolic": "SCADA", 
        "unconnected_send.request.read_frag.elements": 20, 
        "unconnected_send.request.read_frag.offset": 2, 

    We assume that the Message Router will convert the .request to a Response and fill it its .input
    with the encoded response.

    """
    class_id			= 0x06

    UC_SND_REQ			= 0x52 		# Unconnected Send
    FW_OPN_REQ			= 0x54		# Forward Open (unimplemented)
    FW_CLS_REQ			= 0x4E		# Forward Close (unimplemented)


    def request( self, data ):
        """
        Handles an unparsed request.input, parses it and processes the request with the Message Router.
        

        """
        log.detail( "%s Request: %s", self, enip_format( data ))

        # We don't check for Unconnected Send 0x52, because replies (and some requests) don't
        # include the full wrapper, just the raw command.  This is quite confusing; especially since
        # some of the commands have the same code (eg. Read Tag Fragmented, 0x52).  Of course, their
        # replies don't (0x52|0x80 == 0xd2).  The CIP.produce recognizes the absence of the
        # .command, and simply copies the encapsulated request.input as the response payload.  We
        # don't encode the response here; it is done by the UCMM.

        assert 'input' in data.request, \
            "Unconnected Send message with empty request"

        log.info( "%s Parsing: %s", self, enip_format( data.request ))
        # Get the Message Router to parse and process the request into a response, producing a
        # data.request.input encoded response, which we will pass back as our own encoded response.
        MR			= lookup( class_id=0x02, instance_id=1 )
        source			= cpppo.rememberable( data.request.input )
        try: 
            with MR.parser as machine:
                for i,(m,s) in enumerate( machine.run( path='request', source=source, data=data )):
                    log.detail( "%s #%3d -> %10.10s; next byte %3d: %-10.10r: %s",
                                machine.name_centered(), i, s, source.sent, source.peek(),
                                repr( data ) if log.getEffectiveLevel() < logging.DETAIL else reprlib.repr( data ))

            log.info( "%s Executing: %s", self, enip_format( data.request ))
            MR.request( data.request )
        except:
            # Parsing failure.  We're done.  Suck out some remaining input to give us some context.
            processed		= source.sent
            memory		= bytes(bytearray(source.memory))
            pos			= len( source.memory )
            future		= bytes(bytearray( b for b in source ))
            where		= "at %d total bytes:\n%s\n%s (byte %d)" % (
                processed, repr(memory+future), '-' * (len(repr(memory))-1) + '^', pos )
            log.error( "EtherNet/IP CIP error %s\n", where )
            raise

        log.detail( "%s Response: %s", self, enip_format( data ))
        return True
