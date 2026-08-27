import groovy.json.JsonSlurper

def notifySlack(msg) {
    echo "slack: ${msg}"
}

stage('Build') {
    sh 'make'
    notifySlack('build done')
}
stage('Test') {
    sh 'make test'
}

def cleanupHelper() {
    echo 'cleanup'
}
