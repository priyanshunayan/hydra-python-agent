import urllib.request
import json
import logging
from urllib.error import URLError, HTTPError
from hydra_python_core.doc_writer import HydraDoc
from hydra_agent.redis_core.redis_proxy import RedisProxy
from hydra_agent.redis_core.graphutils import GraphUtils
from hydra_agent.redis_core.graph_init import InitialGraph
from redisgraph import Graph, Node
from requests import Session
from typing import Union, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__file__)


class GraphOperations():
    """Responsible to process the requests received by the Agent
    inside Redis Graph, making sure it a synchronized cached layer"""
    def __init__(self, entrypoint_url: str, api_doc: HydraDoc,
                 redis_proxy: RedisProxy):
        """Initialize GraphOperations
        :param entrypoint_url: Entrypoint URL for the hydrus server
        :param api_doc: ApiDoc object that contains the documentation
        :param redis_proxy: RedisProxy object created from redis_proxy module
        :return: None
        """
        self.entrypoint_url = entrypoint_url
        self.api_doc = api_doc
        self.redis_proxy = redis_proxy
        self.redis_connection = redis_proxy.get_connection()
        self.complete_vocabulary_url = self.api_doc.doc_url
        self.vocabulary = self.api_doc.doc_name
        self.graph_utils = GraphUtils(redis_proxy)
        self.redis_graph = Graph("apigraph", self.redis_connection)
        self.session = Session()

    def get_processing(self, url: str, resource: dict) -> list:
        """Synchronize Redis upon new GET operations
        :param url: Resource URL to be updated in Redis.
        :param resource: Resource object fetched from server.
        :return: list of embedded resources to be fetched.
        """
        url_list = url.rstrip('/').replace(self.entrypoint_url, "EntryPoint")
        url_list = url_list.split('/')
        # Updating Redis
        # First case - When processing a GET for a resource
        # When processing GET for a Resource, instead of finding the collection of that resource create a connection
        # in that class only.
        # TODO Make the identification of whether it is a processing GET for resource or Collection
        if len(url_list) == 6:
            resource_endpoint, resource_id = url_list[-2:]
            # Building the the collection id, i.e. vocab:Entrypoint/Collection
            redis_resource_parent_id = self.complete_vocabulary_url.doc_url + 'EntryPoint/' + resource_endpoint

            class_instances = self.graph_utils.read(
                match="",
                where="id='{}'".format(redis_resource_parent_id),
                ret="")

            if 'instances' not in class_instances:
                class_instances = []
            else:
                class_instances = eval(class_instances['instances'])

            resource_to_append = {'@id': resource['@id'],
                                  '@type': resource['@type']}

            class_instances.append(resource_to_append)
            # Updating the collection properties with the nem member
            self.graph_utils.update(
                match="",
                where="id='{}'".format(redis_resource_parent_id),
                set="instances = \"{}\"".format(str(class_instances)))

            # Creating node for new collection member and committing to Redis
            for key, value in resource.items():
                if type(value) is not str:
                    resource[key] = str(value)

            self.graph_utils.add_node("objects" + resource['@type'],
                                      resource['@type'] + resource_id,
                                      resource)
            # Commits the graph
            self.graph_utils.flush()
            # Creating relation between collection node and member
            self.graph_utils.create_relation(label_source="classes",
                                             where_source="type : \'" +
                                             resource_endpoint + "\'",
                                             relation_type="has_" +
                                             resource['@type'],
                                             label_dest="objects" +
                                             resource['@type'],
                                             where_dest="id : \'" +
                                             resource['@id'] + "\'")

            # Checking for embedded resources in the properties of resource
            class_doc = self.api_doc.parsed_classes[resource['@type']]['class']
            class_uris = []
            for class_name, class_def in self.api_doc.parsed_classes.items():
                class_uris.append(class_def['class'].id_)
            supported_properties = class_doc.supportedProperty
            embedded_resources = []
            for supported_prop in supported_properties:
                if supported_prop.prop in class_uris:
                    new_resource = {'parent_id': resource['@id'], 'parent_type': resource['@type'],
                                    'embedded_url': supported_prop.prop}
                    embedded_resources.append(new_resource)

            return embedded_resources
        # Second Case - When processing a GET for a Collection
        elif len(url_list) == 2:
            entrypoint, resource_endpoint = url_list
            redis_collection_id = self.vocabulary + \
                ":" + entrypoint + \
                "/" + resource_endpoint

            self.graph_utils.update(
                match="collection",
                where="id='{}'".format(redis_collection_id),
                set="members = \"{}\"".format(str(resource["members"])))
            return []

        # Third Case - When processing a valid GET that is not compatible-
        # with the Redis Hydra structure, only returns response
        else:
            logger.info("No modification to Redis was made")
            return

    def put_processing(self, url: str, new_object: dict) -> None:
        """Synchronize Redis upon new PUT operations
        :param url: URL for the resource to be created.
        :return: None.
        """
        # Manually add the id that will be on the server for the object added
        url_list = url.split('/', 3)
        new_object["@id"] = '/' + url_list[-1]
        # Simply call self.get_processing to add the resource to the collection at Redis
        embedded_resources = self.get_processing(url, new_object)
        return embedded_resources

    def post_processing(self, url: str, updated_object: dict) -> None:
        """Synchronize Redis upon new POST operations
        :param url: URL for the resource to be updated.
        :return: None.
        """
        # Manually add the id that will be on the server for the object added
        url_list = url.split('/', 3)
        updated_object["@id"] = '/' + url_list[-1]

        # Simply call self.get_processing to add the resource to the collection at Redis
        self.delete_processing(url)
        embedded_resources = self.get_processing(url, updated_object)
        return embedded_resources

    def delete_processing(self, url: str) -> None:
        """Synchronize Redis upon new DELETE operations
        :param url: URL for the resource deleted.
        :return: None.
        """
        # MEMBER NODE Deleting from Redis Graph
        url_list = url.split('/', 3)
        object_id = '/' + url_list[-1]

        self.graph_utils.delete(where="id='{}'".format(object_id))

        # COLLECTION Property members update
        url = url.rstrip('/').replace(self.entrypoint_url, "EntryPoint")
        entrypoint, resource_endpoint, resource_id = url.split('/')

        # Building the the collection id, i.e. vocab:Entrypoint/Collection
        redis_collection_id = self.vocabulary + \
            ":" + entrypoint + \
            "/" + resource_endpoint

        collection_members = self.graph_utils.read(
            match=":collection",
            where="id='{}'".format(redis_collection_id),
            ret="")

        # Checking if it's the first member to be loaded
        if 'members' not in collection_members:
            collection_members = []
        else:
            collection_members = eval(collection_members['members'])

        for member in collection_members:
            if resource_id in member['@id']:
                collection_members.remove(member)

        self.graph_utils.update(
            match="collection",
            where="id='{}'".format(redis_collection_id),
            set="members = \"{}\"".format(str(collection_members)))

        return

    def get_resource(self, url: str = None, initial_graph: InitialGraph = None, resource_type: str = None,
                     filters: dict = {}) -> Union[dict, Optional[list]]:
        """Get resources already stored on Redis and return
        :param url: URL for the resource to fetch.
        :param initial_graph: The Initial Redis graph
        :param resource_type: Type of the resource
        :param filters: filters to apply when searching, resources properties
        :return: Object with resource found.
        """
        # Checking which kind of query, by URL or type
        if not url and not resource_type:
            raise Exception("ERR: You should set at least" +
                            "url OR resource_type")
        if url:
            url_aux = url.rstrip('/')
            url_list = url_aux.split('/')
            # Checking if querying for cached Collection or Member
            if url_list[-1] in initial_graph.collection_endpoints or url_list[-2] in initial_graph.collection_endpoints:
                # When checking for collections we will always fetch the server
                return None
            # since class endpoints will always in the form of /class/<id>
            elif url_list[-2] in initial_graph.class_endpoints:
                object_id = '/' + url_list[-1]
                resource = self.graph_utils.read(
                                    match="",
                                    where="id='{}'".format(object_id),
                                    ret="")
            # If having only one object/querying by id return only dict
                if resource is not None and len(resource) == 1:
                    return resource[0]
        elif resource_type:
            where_filter = ""
            for filter_key, filter_value in filters.items():
                where_filter += " AND p." + filter_key + "=" + \
                                "'{}'".format(filter_value)
            if where_filter:
                where_filter = where_filter.replace(" AND p.", "", 1)

            resource = self.graph_utils.read(
                    match=":objects" +
                    resource_type,
                    where=where_filter,
                    ret="")
            return resource
        else:
            logger.info("get_resource failed and couldn't fetch Redis")

    def link_resources(self, parent_id: str, parent_type: str,
                       node_url: str) -> str:
        """Checks for existence of discovered resource and creates links
        for embedded resources inside other resources properties
        :parent_id: Resource ID for the parent node that had this reference
        :parent_type: Resource Type for the parent node that had this reference
        :node_url: URL Reference for resource found inside a property
        "return: Default Redis response with amount of relations created
        """
        resource = self.get_resource(node_url)
        if resource is None:
            logger.info("\n Embedded link {}".format(node_url) +
                        "cannot be fetched")
            return "\n Embedded link {}".format(node_url) + \
                   "cannot be fetched"

        # Creating relation between collection node and member
        response = self.graph_utils.create_relation(label_source="objects" +
                                                    parent_type,
                                                    where_source="id : \'" +
                                                    parent_id + "\'",
                                                    relation_type="has_" +
                                                    resource['@type'],
                                                    label_dest="objects" +
                                                    resource['@type'],
                                                    where_dest="id : \'" +
                                                    resource['@id'] + "\'")
        return str(response)


if __name__ == "__main__":
    pass
