# (C) Datadog, Inc. 2019
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import json
import re
import os
try:
    # This should be `confluent_kafka` but made by mapr!
    from confluent_kafka import Consumer, KafkaError
except Exception  as e:
    print("Unable to import library `confluent_kafka`, make sure it is installed and LD_LIBRARY_PATH is set correctly")
    # on our infra you can run
    # export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/opt/mapr/lib:/usr/lib/jvm/java-1.8.0-openjdk-1.8.0.222.b10-0.el7_6.x86_64/jre/lib/amd64/server/  # noqa

from six import iteritems

from datadog_checks.base import AgentCheck, ConfigurationError


class MaprCheck(AgentCheck):
    
    def __init__(self, name, init_config, instances):
        super(MaprCheck, self).__init__(name, init_config, instances)
        self._conn = None
        self.hostname = instances[0].get('hostname')
        self.topic_path = instances[0].get('topic_path')
        self.allowed_metrics = [re.compile(w) for w in instances[0].get('whitelist')]

    def check(self, instance):
        mapr_ticketfile_location = instance.get('mapr_ticketfile_location')
        if mapr_ticketfile_location:
            os.environ['MAPR_TICKETFILE_LOCATION'] = mapr_ticketfile_location
        elif not os.environ.get('MAPR_TICKETFILE_LOCATION'):
            raise ConfigurationError(
                "MAPR_TICKETFILE_LOCATION needs to be provided either in config or as an environment variable")

        metrics = {}
        while True:
            m = self.conn.poll(timeout=1.0)
            if m is None:
                # Timed out, no more messages
                break
            if m.error() is None:
                # Metric received
                try:
                    metric = json.loads(m.value().decode('utf-8'))[0]
                    if self.should_collect_metric(metric['metric'], allowed_metrics):
                        metrics[metric['metric']] = {
                            "tags": ["{}:{}".format(k, v) for k, v in iteritems(metric['tags'])],
                            "value": metric['value']
                        }
                except Exception as e:
                    # TODO handle histogran netrics
                    # Error: (mapr.py:45) | Received unexpected message [
                    # {"metric": "mapr.db.table.latency","buckets": {"2,5": 10,"5,10": 21},
                    # "tags": {"table_fid": "2070.36.262534","table_path": "/var/mapr/mapr.monitoring/tsdb",
                    # "noindex": "//primary","rpc_type": "put",
                    # "fqdn": "mapr-lab-2-ghs6.c.datadog-integrations-lab.internal",
                    # "clusterid" : "7616098736519857348",
                    # "clustername" : "demo"}}]
                    self.log.error("Received unexpected message %s", m.value())
            elif m.error().code() != KafkaError._PARTITION_EOF:
                # Real error happened
                self.log.error(m.error())
                break

        # Submit metrics
        # No distinction between gauge and count metrics, this should be hardcoded metric by metric
        for m, props in iteritems(metrics):
            self.gauge(m, props["value"], tags=props["tags"])

    @staticmethod
    def get_stream_id(topic_name, rng=2):
        """To distribute load, all the topics are not in the same stream. Each topic named is hashed
        to obtain an id which is in turn the name of the stream"""
        h = 5381
        for c in topic_name:
            h = ((h << 5) + h) + ord(c)
        return abs(h % rng)

    @property
    def conn(self):
        if self._conn:
            return self._conn

        topic_name = self.host  # According to docs we should append the metric name.
        stream_id = MaprCheck.get_stream_id(topic_name, rng=2)

        # TODO: Make the path configurable
        topic_path = "{}:{}".format(os.path.join(self.topic_path, stream_id), topic_name)
        self._conn = Consumer(
            {
                "group.id": "dd-agent",  # uniquely identify this consumer
                "enable.auto.commit": False  # important, we don't need to store the offset for this consumer,
                # and if we do it just once the mapr library has a bug which prevents reading from the head
            }
        )
        self._conn.subscribe([topic_path])
        return self._conn

    def should_collect_metric(self, metric_name):
        for reg in self.allowed_metrics:
            if re.match(reg, metric_name):
                return True