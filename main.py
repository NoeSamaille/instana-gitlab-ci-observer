import instana
import opentracing
from flask import Flask
from flask import request
from gitlab import Gitlab
import dateutil.parser as parser
import requests
import random
import json
import yaml
import re

# disable urllib3 unsecure ssl warnings
import urllib3
urllib3.disable_warnings()


app = Flask(__name__)

hexids = {
    "gitlab-pipeline": {},
    "gitlab-job": {},
    "gitlab-trace": {},
    "awx-job": {}
}
awx_jobs = {}
config = {}


def get_pipeline(project_id, pipeline_id):
    gl = Gitlab(config["gitlab"]["url"], config["gitlab"]
                ["api-token"], ssl_verify=False)
    gl.auth()

    project = gl.projects.get(project_id)
    pipeline = project.pipelines.get(pipeline_id)
    return pipeline.web_url


def get_job(project_id, job_id):
    gl = Gitlab(config["gitlab"]["url"], config["gitlab"]
                ["api-token"], ssl_verify=False)
    gl.auth()

    project = gl.projects.get(project_id)
    job = project.jobs.get(job_id)
    log = job.trace().decode('utf-8')
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    log = ansi_escape.sub('', log)
    # log='<br>'.join(log.splitlines())

    return job.web_url, log


def awx_get_job_log(job_id):
    authbasic = requests.auth.HTTPBasicAuth(
        config["awx"]["user"], config["awx"]["password"])
    url = f'{config["awx"]["url"]}/api/v2/jobs/{job_id}/stdout?format=txt_download'
    print(url)
    req = requests.get(url, auth=authbasic)
    logs = req.content.decode('utf-8')
    return logs


def load_config():
    global config
    with open("config.yaml", "r") as yamlfile:
        config = yaml.load(yamlfile, Loader=yaml.FullLoader)
        print("Config loaded")
        print(json.dumps(config, indent=4))


def get_id(type, id):
    global hexids
    spanid = ""
    if type not in hexids.keys():
        raise Exception(f"Incorrect type: {type}")

    if id not in hexids[type]:
        print(f"Adding spanid for {type} {id}")
        spanid = '%016x' % random.randrange(16**16)
        hexids[type][id] = spanid
    else:
        print(f"Spanid for {type} {id} already exists: {hexids[type][id]}")
        spanid = hexids[type][id]

    return spanid


@app.post('/awx')
def awx_webhook():
    print("----------------------------- AWX event")
    content = request.get_json(silent=True)

    awx_job_id = get_id("awx-job", content["id"])

    extra_vars_dict = json.dumps(content["extra_vars"])
    extra_vars_dict = json.loads(extra_vars_dict)
    extra_vars_dict = json.loads(extra_vars_dict)

    pipeline_id = int(extra_vars_dict["pipeline_id"])
    job_id = int(extra_vars_dict["job_id"])

    awx_job_start_date = parser.parse(content["started"])
    print(f"{content['name']} {awx_job_start_date}")
    awx_job_end_date = parser.parse(content["finished"])
    awx_job_start_time = awx_job_start_date.timestamp()
    awx_job_duration = (awx_job_end_date-awx_job_start_date).total_seconds()
    awx_job_name = f"AWX Job {content['project']}/{content['name']}/{content['playbook']}"
    awx_job_log = awx_get_job_log(content["id"])

    if content["status"] in ('successful'):
        awx_job_error = False
    else:
        awx_job_error = True
    
    awx_jobs[f'{pipeline_id}-{job_id}'] = {
        "id": awx_job_id,
        "start_time": awx_job_start_time,
        "duration": awx_job_duration,
        "name": awx_job_name,
        "error": awx_job_error,
        "logs": awx_job_log,
        "url": content["url"]
    }

    return 'OK\n'


@app.post('/gitlab-ci')
def index():
    if "webhook-token" not in config["gitlab"] or config["gitlab"]["webhook-token"] == "" or config["gitlab"]["webhook-token"] == request.headers['X-Gitlab-Event']:

        content = request.get_json(silent=True)

        if content["object_kind"] == "pipeline":
            pipeline_id = content["object_attributes"]["id"]

            if content["object_attributes"]["status"] in ("success", "failed"):
                print("----------------------------- Gitlab event -> Final Event")

                project_id = content['project']['id']
                pipeline_start_date = parser.parse(
                    content["object_attributes"]["created_at"])
                pipeline_end_date = parser.parse(
                    content["object_attributes"]["finished_at"])
                pipeline_start_time = pipeline_start_date.timestamp()
                pipeline_duration = (
                    pipeline_end_date-pipeline_start_date).total_seconds()
                pipeline_name = f"Pipeline {content['project']['path_with_namespace']}/{content['object_attributes']['id']}"
                if content["object_attributes"]["status"] in ('failed'):
                    pipeline_error = True
                else:
                    pipeline_error = False
                pipeline_url = get_pipeline(project_id, pipeline_id)
                print(f"{pipeline_name} {pipeline_start_date}")

                pipeline_span = opentracing.tracer.start_span(
                    pipeline_name, start_time=pipeline_start_time)
                pipeline_span.set_tag('span.kind', 'entry')
                pipeline_span.set_tag('url', pipeline_url)
                pipeline_span.set_tag('error', pipeline_error)

                for job in content["builds"]:
                    if job["status"] in ("success", "failed"):
                        job_id = job["id"]
                        job_start_date = parser.parse(job["started_at"])
                        job_start_time = job_start_date.timestamp()
                        job_duration = int(job["duration"])
                        job_name = f'Job {job["name"]}'
                        if job["status"] in ('failed'):
                            job_error = True
                        else:
                            job_error = False
                        job_url, job_log = get_job(project_id, job_id)

                        job_span = opentracing.tracer.start_span(
                            job_name, start_time=job_start_time, child_of=pipeline_span)
                        job_span.set_tag('span.kind', 'exit')
                        job_span.set_tag('url', job_url)
                        job_span.set_tag('error', job_error)
                        job_span.finish(job_start_time+job_duration)

                        print(f"{job_name} {job_start_date}")
                        if job["status"] in ('failed'):
                            log_span = opentracing.tracer.start_span(
                                'log', start_time=job_start_time+job_duration, child_of=job_span)
                            job_span.set_tag('span.kind', 'exit')
                            # log_span.log_kv({'message':'mind the hole'}) # Warning log
                            log_span.log_exception(job_log[-10000:])
                            log_span.finish(job_start_time+job_duration+.001)
                        
                        # Handles (if applicable) related AWX job
                        if f'{pipeline_id}-{job_id}' in awx_jobs:
                            payload = awx_jobs[f'{pipeline_id}-{job_id}']
                            awx_job_span = opentracing.tracer.start_span(
                                payload['name'], start_time=payload['start_time'], child_of=job_span)
                            awx_job_span.set_tag('span.kind', 'exit')
                            awx_job_span.set_tag('url', payload['url'])
                            awx_job_span.set_tag('error', payload['error'])
                            awx_job_span.set_tag('name', payload['name'])
                            awx_job_span.finish(payload['start_time']+payload['duration'])
                            if payload['error']:
                                aws_log_span = opentracing.tracer.start_span(
                                    'log', start_time=payload['start_time']+payload['duration'], child_of=awx_job_span)
                                aws_log_span.set_tag('span.kind', 'exit')
                                # log_span.log_kv({'message':'mind the hole'}) # Warning log
                                aws_log_span.log_exception(payload['logs'][-10000:])
                                aws_log_span.finish(payload['start_time']+payload['duration']+.001)
                            awx_jobs.pop(f'{pipeline_id}-{job_id}', None)

                pipeline_span.finish(
                    pipeline_start_time+pipeline_duration)

        return 'OK\n'


if __name__ == "__main__":
    load_config()
    app.run(host='0.0.0.0', port=8088, debug=True)
