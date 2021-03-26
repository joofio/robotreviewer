"""
RobotReviewer ML worker

called by `celery -A ml_worker worker --loglevel=info`

"""


# Authors:  Iain Marshall <mail@ijmarshall.com>
#           Joel Kuiper <me@joelkuiper.com>
#           Byron Wallace <byron@ccs.neu.edu>


from celery import Celery, current_task
from celery.contrib import rdb
from celery.signals import worker_init
import json
import logging, os

import sqlite3
def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")


DEBUG_MODE = str2bool(os.environ.get("DEBUG", "true"))
LOCAL_PATH = "robotreviewer/uploads"
LOG_LEVEL = (logging.DEBUG if DEBUG_MODE else logging.INFO)
# determined empirically by Edward; covers 90% of abstracts
# (crudely and unscientifically adjusted for grobid)
NUM_WORDS_IN_ABSTRACT = 450
import robotreviewer
from robotreviewer import config
logging.basicConfig(level=LOG_LEVEL, format='[%(levelname)s] %(name)s %(asctime)s: %(message)s', filename=robotreviewer.get_data(config.LOG))
log = logging.getLogger(__name__)

log.info("RobotReviewer machine learning tasks starting")


from robotreviewer.textprocessing.pdfreader import PdfReader
pdf_reader = PdfReader() # launch Grobid process before anything else


from robotreviewer.textprocessing.tokenizer import nlp

''' robots! '''
# from robotreviewer.robots.bias_robot import BiasRobot
from robotreviewer.robots.rationale_robot import BiasRobot
from robotreviewer.robots.pico_robot import PICORobot
from robotreviewer.robots.rct_robot import RCTRobot
from robotreviewer.robots.pubmed_robot import PubmedRobot
from robotreviewer.robots.pico_span_robot import PICOSpanRobot
from robotreviewer.robots.bias_ab_robot import BiasAbRobot
from robotreviewer.robots.human_robot import HumanRobot
from robotreviewer.robots.mesh_robot import MeshRobot
# from robotreviewer.robots.mendeley_robot import MendeleyRobot
# from robotreviewer.robots.ictrp_robot import ICTRPRobot
# from robotreviewer.robots import pico_viz_robot
# from robotreviewer.robots.pico_viz_robot import PICOVizRobot
from robotreviewer.robots.punchlines_robot import PunchlinesBot
from robotreviewer.robots.sample_size_robot import SampleSizeBot
from robotreviewer.robots.inference_robot import InferenceRobot

from robotreviewer.data_structures import MultiDict


import robotreviewer


######
## default annotation pipeline defined here
######
'''
log.info("Loading the robots...")
bots = {"bias_bot": BiasRobot(top_k=3),
        "pico_bot": PICORobot(),
        "pubmed_bot": PubmedRobot(),
        # "ictrp_bot": ICTRPRobot(),
        "rct_bot": RCTRobot(),
        #"pico_viz_bot": PICOVizRobot(),
        "sample_size_bot":SampleSizeBot()}

log.info("Robots loaded successfully! Ready...")
'''

# lastly wait until Grobid is connected
pdf_reader.connect()

# start up Celery service
app = Celery('ml_worker', backend='amqp://', broker='amqp://')

#####
## connect to and set up database
#####
rr_sql_conn = sqlite3.connect(robotreviewer.get_data('uploaded_pdfs/uploaded_pdfs.sqlite'), detect_types=sqlite3.PARSE_DECLTYPES,  check_same_thread=False)


c = rr_sql_conn.cursor()
c.execute('CREATE TABLE IF NOT EXISTS doc_queue(id INTEGER PRIMARY KEY, report_uuid TEXT, pdf_uuid TEXT, pdf_hash TEXT, pdf_filename TEXT, pdf_file BLOB, timestamp TIMESTAMP)')
c.execute('CREATE TABLE IF NOT EXISTS api_queue(id INTEGER PRIMARY KEY, report_uuid TEXT, uploaded_data TEXT, timestamp TIMESTAMP)')
c.execute('CREATE TABLE IF NOT EXISTS api_done(id INTEGER PRIMARY KEY, report_uuid TEXT, annotations TEXT, timestamp TIMESTAMP)')
c.execute('CREATE TABLE IF NOT EXISTS article(id INTEGER PRIMARY KEY, report_uuid TEXT, pdf_uuid TEXT, pdf_hash TEXT, pdf_file BLOB, annotations TEXT, timestamp TIMESTAMP, dont_delete INTEGER)')
c.close()
rr_sql_conn.commit()


@worker_init.connect
def on_worker_init(**_):
    global bots
    global friendly_bots
    global inf_bot 

    log.info("Loading the robots...")

    # pico span bot must be loaded first i have *no* idea why...
    print("LOADING ROBOTS")
    bots = {"pico_span_bot": PICOSpanRobot(),
            "bias_bot": BiasRobot(top_k=3),
            "pico_bot": PICORobot(),
            "pubmed_bot": PubmedRobot(),
            # "ictrp_bot": ICTRPRobot(),
            "rct_bot": RCTRobot(),
            "mesh_bot": MeshRobot(),
            #"pico_viz_bot": PICOVizRobot(),
            "punchline_bot":PunchlinesBot(),
            "sample_size_bot":SampleSizeBot(),
            "bias_ab_bot": BiasAbRobot(),
            "human_bot": HumanRobot()}

    friendly_bots = {"pico_span_bot": "Extracting PICO text from title/abstract",
                     "bias_bot": "Assessing risks of bias",
                     "pico_bot": "Extracting PICO information from full text",
                     "rct_bot": "Assessing study design (is it an RCT?)",
                     "sample_size_bot": "Extracting sample size",
                     "mesh_bot": "Extracting MeSH terms",
                     "punchline_bot": "Extracting main conclusions",
                     "pubmed_bot": "Looking up meta-data in PubMed",
                     "bias_ab_bot": "Assessing bias from abstract"}

    # this requires assembling outputs from ICO bot, so we keep
    # separate from pipeline for now
    inf_bot = InferenceRobot()

    print("ROBOTS ALL LOADED")
    log.info("Robots loaded successfully! Ready...")

@app.task
def pdf_annotate(report_uuid):
    """
    takes a report uuid as input
    searches for pdfs using that id,
    then saves annotations in database
    """
    pdf_uuids, pdf_hashes, filenames, blobs, timestamps = [], [], [], [], []

    c = rr_sql_conn.cursor()

    # load in the PDF data from the queue table
    for pdf_uuid, pdf_hash, filename, pdf_file, timestamp in c.execute("SELECT pdf_uuid, pdf_hash, pdf_filename, pdf_file, timestamp FROM doc_queue WHERE report_uuid=?", (report_uuid, )):
        pdf_uuids.append(pdf_uuid)
        pdf_hashes.append(pdf_hash)
        filenames.append(filename)
        blobs.append(pdf_file)
        timestamps.append(timestamp)

    c.close()

    current_task.update_state(state='PROGRESS', meta={'process_percentage': 25, 'task': 'reading PDFs'})
    articles = pdf_reader.convert_batch(blobs)
    parsed_articles = []


    current_task.update_state(state='PROGRESS', meta={'process_percentage': 50, 'task': 'parsing text'})
    # tokenize full texts here
    for doc in nlp.pipe((d.get('text', u'') for d in articles), batch_size=1, n_threads=config.SPACY_THREADS):
        parsed_articles.append(doc)



    # adjust the tag, parse, and entity values if these are needed later
    for article, parsed_text in zip(articles, parsed_articles):
        article._spacy['parsed_text'] = parsed_text

    current_task.update_state(state='PROGRESS',meta={'process_percentage': 75, 'task': 'doing machine learning'})


    for pdf_uuid, pdf_hash, filename, blob, data, timestamp in zip(pdf_uuids, pdf_hashes, filenames, blobs, articles, timestamps):


        # DEBUG
        current_task.update_state(state='PROGRESS',meta={'process_percentage': 76, 'task': 'processing PDF {}'.format(filename)})


        #  "punchline_bot",
        data = pdf_annotate_study(data, bot_names=["rct_bot", "pubmed_bot", "bias_bot", "pico_bot", "pico_span_bot", "punchline_bot", "sample_size_bot"])



        data.gold['pdf_uuid'] = pdf_uuid
        data.gold['filename'] = filename
        c = rr_sql_conn.cursor()
        c.execute("INSERT INTO article (report_uuid, pdf_uuid, pdf_hash, pdf_file, annotations, timestamp, dont_delete) VALUES(?, ?, ?, ?, ?, ?, ?)", (report_uuid, pdf_uuid, pdf_hash, sqlite3.Binary(blob), data.to_json(), timestamp, config.DONT_DELETE))
        rr_sql_conn.commit()
        c.close()

    # finally delete the PDFs from the queue
    c = rr_sql_conn.cursor()
    c.execute("DELETE FROM doc_queue WHERE report_uuid=?", (report_uuid, ))
    rr_sql_conn.commit()
    c.close()
    current_task.update_state(state='SUCCESS', meta={'process_percentage': 100, 'task': 'done!'})
    return {"process_percentage": 100, "task": "completed"}





@app.task
def api_annotate(report_uuid):
    """
    Handles annotation tasks sent from the API
    Strict in datatype handling
    """

    current_task.update_state(state='PROGRESS', meta={
        'status': "in process",
        'position': "received request, fetching data"}
    )


    c = rr_sql_conn.cursor()

    c.execute("SELECT uploaded_data, timestamp FROM api_queue WHERE report_uuid=?", (report_uuid, ))
    result = c.fetchone()
    uploaded_data_s, timestamp = result
    uploaded_data = json.loads(uploaded_data_s)



    articles = uploaded_data["articles"]
    target_robots = uploaded_data["robots"]
    filter_rcts = uploaded_data.get("filter_rcts", "is_rct_balanced")



    # now do the ML
    if filter_rcts != 'none':

        current_task.update_state(state='PROGRESS', meta={
            'status': "in process",
            'position': "rct_robot classification"}
        )

        # do rct_bot first
        results = bots['rct_bot'].api_annotate(articles)
        for a, r in zip(articles, results):
            if r[filter_rcts]:
                a['skip_annotation'] = False
            else:
                a['skip_annotation'] = True
            a['rct_bot'] = r

        # and remove from the task list if present so don't duplicate
        target_robots = [tr for tr in target_robots if tr != "rct_bot"]

    current_task.update_state(state='PROGRESS', meta={
        'status': "in process",
        'position': "tokenizing data"}
    )


    for k in ["ti", "ab", "fullText"]:

        parsed = nlp.pipe((a.get(k, "") for a in articles if a.get('skip_annotation', False)==False))
        articles_gen = (a for a in articles)

        while True:
            try:
                current_doc = articles_gen.__next__()
            except StopIteration:
                break
            if current_doc.get("skip_annotation"):
                continue
            else:
                current_doc['parsed_{}'.format(k)] = parsed.__next__()



    for bot_name in target_robots:
        current_task.update_state(state='PROGRESS', meta={
           'status': "in process",
            'position': "{} classification".format(bot_name)}
        )
        results = bots[bot_name].api_annotate(articles)
        for a, r in zip(articles, results):
            if not a.get('skip_annotations', False):
                a[bot_name] = r

    # delete the parsed text
    for article in articles:
        for k in ["ti", "ab", "fullText"]:
            article.pop('parsed_{}'.format(k), None)
    c = rr_sql_conn.cursor()

    current_task.update_state(state='PROGRESS', meta={
           'status': "in process",
            'position': "writing the predictions to database"}
    )

    c.execute("INSERT INTO api_done (report_uuid, annotations, timestamp) VALUES(?, ?, ?)", (report_uuid, json.dumps(articles), timestamp))
    rr_sql_conn.commit()
    c.close()

    # finally delete the data from the queue
    c = rr_sql_conn.cursor()
    c.execute("DELETE FROM api_queue WHERE report_uuid=?", (report_uuid, ))
    rr_sql_conn.commit()
    c.close()
    current_task.update_state(state='done')
    return {"status": 100, "task": "completed"}



def pdf_annotate_study(data, bot_names=["bias_bot"]):
    #
    # ANNOTATION TAKES PLACE HERE
    # change the line below if you wish to customise or
    # add a new annotator
    #
    log.info("REQUESTING ANNOTATIONS FROM SET OF PDFs (annotate_study)")
    annotations = pdf_annotation_pipeline(bot_names, data)
    return annotations


def pdf_annotation_pipeline(bot_names, data):
    # makes it here!
    log.info("STARTING PIPELINE (made it to annotation_pipeline)")

        # pico span bot must be loaded first i have *no* idea why...
    log.info("LOADING ROBOTS -> V2")
    bots = {"pico_span_bot": PICOSpanRobot(),
            "bias_bot": BiasRobot(top_k=3),
            "pico_bot": PICORobot(),
            "pubmed_bot": PubmedRobot(),
            # "ictrp_bot": ICTRPRobot(),
            "rct_bot": RCTRobot(),
            "mesh_bot": MeshRobot(),
            #"pico_viz_bot": PICOVizRobot(),
            "punchline_bot":PunchlinesBot(),
            "sample_size_bot":SampleSizeBot(),
            "bias_ab_bot": BiasAbRobot(),
            "human_bot": HumanRobot()}

    # DEBUG
    current_task.update_state(state='PROGRESS',meta={'process_percentage': 78, 'task': 'starting annotation pipeline'})


    for bot_name in bot_names:
        log.info("STARTING {} BOT (annotation_pipeline)".format(bot_name))
        log.debug("Sending doc to {} for annotation...".format(bots[bot_name].__class__.__name__))
        current_task.update_state(state='PROGRESS', meta={'process_percentage': 79, 'task': friendly_bots[bot_name]})

        data = bots[bot_name].pdf_annotate(data)
        log.debug("{} done!".format(bots[bot_name].__class__.__name__))
        log.info("COMPLETED {} BOT (annotation_pipeline)".format(bot_name))
        # current_task.update_state(state='PROGRESS',meta={'process_percentage': 79, 'task': 'Bot {} complete!'.format(bot_name)})
    
    
    log.info("running inference...")

    try:
        # note that this will simplyy fail if abstract or pmid is unavailable
        packaged_data = [{"pmid": data["pmid"], "abstract": str(data["abstract"]), 
                         "p": data['pico_span']['population'], "i": data['pico_span']['interventions'], 
                         "o": data['pico_span']['outcomes'] }]
        inference_res = inf_bot.annotate(packaged_data)
        log.info("success! {}".format(packaged_data[0]))
        data["ico_results"] = packaged_data[0]['icos']
    except: 
        log.debug("inference call failed on {}".format(data))

    return data
