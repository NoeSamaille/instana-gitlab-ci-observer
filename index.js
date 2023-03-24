const instana = require('@instana/collector')({
    tracing: {
        automaticTracingEnabled: false
    }
});

const express = require('express')
const app = express()
const bunyan = require('bunyan');
const logger = bunyan.createLogger({ name: "myapp", level: 'warn' });
instana.setLogger(logger);
const port = 8088

app.get('/', (req, res) => {
    instana.sdk.promise.startEntrySpan('my-custom-span-promise').then(() => {
        logger.error('Yay! 🎉');
        logger.warn('Yay Warn! 🎉');
        res.status(500).json({err: 'Error'})
        instana.sdk.promise.completeEntrySpan();
      }).catch(err => {
        instana.sdk.promise.completeEntrySpan(err);
        logger.error(err);
      });
});

app.listen(port, () => {
    console.log(`Example app listening on port ${port}`)
});
