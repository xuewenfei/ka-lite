import copy
import datetime
import json
import os
import requests
import shutil
import time

from math import ceil, log, exp  # needed for basepoints calculation

from django.conf import settings; logging = settings.LOG

from fle_utils.general import datediff

from django.utils.text import slugify

def retrieve_API_data(channel=None):

    topic_tree = {}

    exercises = []

    videos = []

    assessment_items = []

    content = []

    return topic_tree, exercises, videos, assessment_items, content

def whitewash_node_data(node, path="", ancestor_ids=[], channel_data={}):
    """
    Utility function to convert nodes into the format used by KA Lite.
    Extracted from other functions so as to be reused by both the denormed
    and fully inflated exercise and video nodes.
    """

    kind = node.get("kind", None)

    if not kind:
        return node

    node["x_pos"] = node.get("x_pos", 0) or node.get("h_position", 0)
    node["y_pos"] = node.get("y_pos", 0) or node.get("v_position", 0)

    # Only keep key data we can use
    if channel_data["attribute_whitelists"].has_key(kind):
        for key in node.keys():
            if key not in channel_data["attribute_whitelists"][kind]:
                del node[key]

    # Fix up data
    if channel_data["slug_key"][kind] not in node:
        node[channel_data["slug_key"][kind]] = node["id"]

    node["slug"] = node[channel_data["slug_key"][kind]] if node[channel_data["slug_key"][kind]] != "root" else ""
    node["slug"] = slugify(unicode(node["slug"]))
    if not node.has_key("id"):
        node["id"] = node[channel_data["id_key"][kind]]

    node["path"] = path + node["slug"] + "/"
    if not node.has_key("title"):
        node["title"] = (node.get(channel_data["title_key"][kind], ""))
    node["title"] = node["title"].strip()

    # Add some attribute that should have been on there to start with.
    node["parent_id"] = ancestor_ids[-1] if ancestor_ids else None
    node["ancestor_ids"] = ancestor_ids

    if kind == "Video":
        # TODO: map new videos into old videos; for now, this will do nothing.
        node["video_id"] = node["youtube_id"]

    elif kind == "Exercise":
        # For each exercise, need to set the exercise_id
        #   get related videos
        #   and compute base points
        node["exercise_id"] = node["slug"]

        # compute base points
        # Minimum points per exercise: 5
        node["basepoints"] = ceil(7 * log(max(exp(5./7), node.get("seconds_per_fast_problem", 0))));

    return node

def rebuild_topictree(
    remove_unknown_exercises=False,
    remove_disabled_topics=True,
    whitewash_node_data=whitewash_node_data,
    retrieve_API_data=retrieve_API_data,
    channel_data={},
    channel=None
    ):
    """Downloads topictree (and supporting) data from Khan Academy and uses it to
    rebuild the KA Lite topictree cache (topics.json).
    """

    topic_tree, exercises, videos, assessment_items, content = retrieve_API_data(channel=channel)

    exercise_lookup = {exercise["id"]: exercise for exercise in exercises}

    video_lookup = {video["id"]: video for video in videos}

    def recurse_nodes(node, path="", ancestor_ids=[]):
        """
        Internal function for recursing over the topic tree, marking relevant metadata,
        and removing undesired attributes and children.
        """

        kind = node["kind"]

        node = whitewash_node_data(node, path, ancestor_ids)

        if kind != "Topic":
            if channel_data["denormed_attribute_list"].has_key(kind):
                for key in node.keys():
                    if key not in channel_data["denormed_attribute_list"][kind] or not node.get(key, ""):
                        del node[key]

        # Loop through children, remove exercises and videos to reintroduce denormed data
        children_to_delete = []
        child_kinds = set()
        for i, child in enumerate(node.get("children", [])):
            child_kind = child.get("kind", None)

            if child_kind=="Video" or child_kind=="Exercise":
                children_to_delete.append(i)

        for i in reversed(children_to_delete):
            # Reversing means that earlier indices are unaffected by deletion of later ones.
            del node["children"][i]

        # Loop through child_data to populate children with denormed data of exercises and videos.
        for child_datum in node.get("child_data", []):
            try:
                if child_datum["kind"] == "Exercise":
                    child_denormed_data = exercise_lookup[str(child_datum["id"])]
                elif child_datum["kind"] == "Video":
                    child_denormed_data = video_lookup[str(child_datum["id"])]
                else:
                    child_denormed_data = None
                if child_denormed_data:
                    node["children"].append(copy.deepcopy(dict(child_denormed_data)))
            except KeyError as e:
                logging.warn("%(kind)s %(id)s does not exist in lookup table" % child_datum)


        # Recurse through children, remove any blacklisted items
        children_to_delete = []
        child_kinds = set()
        for i, child in enumerate(node.get("children", [])):
            child_kind = child.get("kind", None)

            # Blacklisted--remove
            if child_kind in channel_data["kind_blacklist"]:
                children_to_delete.append(i)
                continue
            elif child[channel_data["slug_key"][child_kind]] in channel_data["slug_blacklist"]:
                children_to_delete.append(i)
                continue
            elif not child.get("live", True) and remove_disabled_topics:  # node is not live
                logging.debug("Removing non-live child: %s" % child[channel_data["slug_key"][child_kind]])
                children_to_delete.append(i)
                continue
            elif child.get("hide", False) and remove_disabled_topics:  # node is hidden. Note that root is hidden, and we're implicitly skipping that.
                children_to_delete.append(i)
                logging.debug("Removing hidden child: %s" % child[channel_data["slug_key"][child_kind]])
                continue
            elif child_kind == "Video" and set(["mp4", "png"]) - set(child.get("download_urls", {}).keys()):
                # for now, since we expect the missing videos to be filled in soon,
                #   we won't remove these nodes
                logging.warn("No download link for video: %s\n" % child["youtube_id"])
                children_to_delete.append(i)
                continue

            child_kinds = child_kinds.union(set([child_kind]))
            child_kinds = child_kinds.union(recurse_nodes(child, path=node["path"], ancestor_ids=ancestor_ids + [node["id"]]))

        # Delete those marked for completion
        for i in reversed(children_to_delete):
            # Reversing means that earlier indices are unaffected by deletion of later ones.
            del node["children"][i]

        # Mark on topics whether they contain Videos, Exercises, or both
        if kind == "Topic":
            node["contains"] = list(child_kinds)

        return child_kinds
    recurse_nodes(topic_tree)

    def recurse_nodes_to_remove_childless_nodes(node):
        """
        Remove dead-end topics.
        """
        children_to_delete = []
        for ci, child in enumerate(node.get("children", [])):
            if child["kind"] != "Topic":
                continue

            recurse_nodes_to_remove_childless_nodes(child)

            if not child.get("children"):
                children_to_delete.append(ci)
                logging.warn("Removing childless topic: %s" % child["slug"])

        for ci in reversed(children_to_delete):
            del node["children"][ci]
    recurse_nodes_to_remove_childless_nodes(topic_tree)

    def recurse_nodes_to_add_position_data(node):
        """
        Only execises have position data associated with them.
        To get position data for higher level topics, averaging of
        lower level positions can be used to give a center of mass.
        """
        if node["kind"] == "Topic":
            x_pos = []
            y_pos = []
            videos = []
            for child in node.get("children", []):
                if not (child.get("x_pos", 0) and child.get("y_pos", 0)):
                    recurse_nodes_to_add_position_data(child)
                if child.get("x_pos", 0) and child.get("y_pos", 0):
                    x_pos.append(child["x_pos"])
                    y_pos.append(child["y_pos"])
                elif child["kind"] == "Video":
                    videos.append(child)
            if len(x_pos) and len(y_pos):
                node["x_pos"] = sum(x_pos)/float(len(x_pos))
                node["y_pos"] = sum(y_pos)/float(len(y_pos))
                for i, video in enumerate(videos):
                    video["x_pos"] = min(x_pos) + (max(x_pos) - min(x_pos))*i/float(len(videos))
                    video["y_pos"] = min(y_pos) + (max(y_pos) - min(y_pos))*i/float(len(videos))

    recurse_nodes_to_add_position_data(topic_tree)

    return topic_tree, exercises, videos, assessment_items, content

def recurse_topic_tree_to_create_hierarchy(node, level_cache={}, hierarchy=[]):
    if not level_cache:
        for hier in hierarchy:
            level_cache[hier] = []
    render_type = node.get("render_type", "")
    if render_type in hierarchy:
        node_copy = copy.deepcopy(dict(node))
        for child in node_copy.get("children", []):
            if child.has_key("children"):
                del child["children"]
        level_cache[render_type].append(node_copy)
    for child in node.get("children", []):
        recurse_topic_tree_to_create_hierarchy(child, level_cache, hierarchy=hierarchy)
    return level_cache